`default_nettype none

// Wide-lane per-row field ALU: SLOTS whole 16-byte quad rows per beat, slot
// s at [128*s +: 128], with each row laid out as {aux1, aux0, value, key}
// (field i at bits [32*i +: 32]). The configured dau_map_alu_pkg transform
// is applied independently to every non-pad row; the reserved ALL-ONES
// 128-bit PAD passes through unchanged. A globally stalled PIPELINE_DEPTH-
// stage pipeline preserves elastic valid/ready flow, positions, and input_last.
//
// Config latches on the first beat of each batch. Invalid config drains the
// batch and reports ERR_CONFIG, matching the 64-bit row tile. Whole-row
// beats cannot tear, so there is no ERR_STREAM path. This composed-pipeline
// stage is silent on success; the downstream tile's close-out is
// authoritative.
module dau_int32_wide_row_map_alu
    import dau_map_alu_pkg::*;
#(
    parameter int SLOTS = 4,          // quad rows per beat: 1, 2, or 4
    parameter int PIPELINE_DEPTH = 3  // elastic stages: 1 through 8
) (
    input  wire logic                         clk,
    input  wire logic                         rst,
    input  wire logic [2:0]                   cfg_op,
    input  wire logic [1:0]                   cfg_dst_field,
    input  wire logic [1:0]                   cfg_src_a_field,
    input  wire logic [1:0]                   cfg_src_b_field,
    input  wire logic [4:0]                   cfg_shift,
    input  wire logic                         input_valid,
    output logic                              input_ready,
    input  wire logic [SLOTS*128-1:0]         input_data,
    input  wire logic                         input_last,
    output logic                              output_valid,
    input  wire logic                         output_ready,
    output logic [SLOTS*128-1:0]              output_data,
    output logic                              output_last,
    output logic                              status_valid,
    input  wire logic                         status_ready,
    output logic                              status_error,
    output logic [7:0]                        status_error_code
);
    if (!(SLOTS == 1 || SLOTS == 2 || SLOTS == 4)) begin : g_bad_slots
        $error("SLOTS must be 1, 2, or 4");
    end
    if (PIPELINE_DEPTH < 1 || PIPELINE_DEPTH > 8) begin : g_bad_pipeline_depth
        $error("PIPELINE_DEPTH must be between 1 and 8");
    end

    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [127:0] PAD_ROW = {128{1'b1}};

    typedef enum logic [1:0] {
        STATE_MAP,     // accept and map one beat of SLOTS rows per cycle
        STATE_DISCARD, // bad config: consume the batch to input_last
        STATE_STATUS   // hold an error close-out until accepted
    } state_t;

    state_t state;

    logic       batch_open;
    logic       batch_input_closed;
    logic [2:0] active_op;
    logic [1:0] active_dst;
    logic [1:0] active_src_a;
    logic [1:0] active_src_b;
    logic [4:0] active_shift;

    logic advance;
    logic input_fire;
    logic output_fire;
    logic first_beat;
    logic config_ok;
    logic accepted_map_beat;

    logic [PIPELINE_DEPTH-1:0] pipeline_valid;
    logic [PIPELINE_DEPTH-1:0] pipeline_last;
    logic [SLOTS*128-1:0] pipeline_data [0:PIPELINE_DEPTH-1];
    logic [SLOTS*128-1:0] computed_data;

    assign advance = !output_valid || output_ready;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign input_ready = advance && (((state == STATE_MAP) && !batch_input_closed)
        || (state == STATE_DISCARD));

    assign first_beat = !batch_open;
    assign accepted_map_beat = input_fire && (state == STATE_MAP)
        && (!first_beat || config_ok);
    assign output_valid = pipeline_valid[PIPELINE_DEPTH-1];
    assign output_data = pipeline_data[PIPELINE_DEPTH-1];
    assign output_last = pipeline_last[PIPELINE_DEPTH-1];

    assign config_ok = cfg_op != 3'd0;

    function automatic logic [31:0] field_select(input logic [1:0] sel, input logic [127:0] row);
        begin
            unique case (sel)
                2'd0: field_select = row[31:0];
                2'd1: field_select = row[63:32];
                2'd2: field_select = row[95:64];
                default: field_select = row[127:96];
            endcase
        end
    endfunction

    function automatic logic [127:0] mapped_row(
        input logic [127:0] row,
        input logic [2:0] op,
        input logic [1:0] dst,
        input logic [1:0] src_a,
        input logic [1:0] src_b,
        input logic [4:0] shift
    );
        logic signed [63:0] product;
        logic [31:0] operand_a;
        logic [31:0] operand_b;
        logic [31:0] cheap;
        logic [31:0] result;
        begin
            mapped_row = row;
            operand_a = field_select(src_a, row);
            operand_b = field_select(src_b, row);
            product = signed'(operand_a) * signed'(operand_b);
            cheap = map_cheap_result(op, operand_a, operand_b, shift);
            result = map_finish_sample(op, product, cheap, shift);
            if (row != PAD_ROW) begin
                unique case (dst)
                    2'd0: mapped_row[31:0] = result;
                    2'd1: mapped_row[63:32] = result;
                    2'd2: mapped_row[95:64] = result;
                    default: mapped_row[127:96] = result;
                endcase
            end
        end
    endfunction

    always_comb begin
        computed_data = input_data;
        for (int s = 0; s < SLOTS; s++) begin
            computed_data[128*s +: 128] = mapped_row(
                input_data[128*s +: 128],
                first_beat ? cfg_op : active_op,
                first_beat ? cfg_dst_field : active_dst,
                first_beat ? cfg_src_a_field : active_src_a,
                first_beat ? cfg_src_b_field : active_src_b,
                first_beat ? cfg_shift : active_shift
            );
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (advance) begin
                for (int stage = PIPELINE_DEPTH - 1; stage > 0; stage--) begin
                    pipeline_valid[stage] <= pipeline_valid[stage-1];
                    pipeline_data[stage] <= pipeline_data[stage-1];
                    pipeline_last[stage] <= pipeline_last[stage-1];
                end
                pipeline_valid[0] <= accepted_map_beat;
                pipeline_data[0] <= computed_data;
                pipeline_last[0] <= input_last;
            end

            if (output_fire && output_last) begin
                batch_open <= 1'b0;
                batch_input_closed <= 1'b0;
            end
            case (state)
                STATE_MAP: begin
                    if (input_fire) begin
                        if (first_beat) begin
                            batch_open <= 1'b1;
                            active_op <= cfg_op;
                            active_dst <= cfg_dst_field;
                            active_src_a <= cfg_src_a_field;
                            active_src_b <= cfg_src_b_field;
                            active_shift <= cfg_shift;
                        end
                        if (first_beat && !config_ok) begin
                            // config precedence over stream shape: drain the
                            // batch, then CONFIG
                            if (input_last) begin
                                raise_error(ERR_CONFIG);
                            end else begin
                                state <= STATE_DISCARD;
                            end
                        end else begin
                            if (input_last) begin
                                batch_input_closed <= 1'b1;
                            end
                        end
                    end
                end
                STATE_DISCARD: begin
                    if (input_fire && input_last) begin
                        raise_error(ERR_CONFIG);
                    end
                end
                STATE_STATUS: begin
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= STATE_MAP;
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    task automatic raise_error(input logic [7:0] code);
        begin
            batch_open <= 1'b0;
            batch_input_closed <= 1'b0;
            pipeline_valid <= '0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_MAP;
            batch_open <= 1'b0;
            batch_input_closed <= 1'b0;
            active_op <= 3'd0;
            active_dst <= 2'd0;
            active_src_a <= 2'd0;
            active_src_b <= 2'd0;
            active_shift <= 5'd0;
            pipeline_valid <= '0;
            pipeline_last <= '0;
            for (int stage = 0; stage < PIPELINE_DEPTH; stage++) begin
                pipeline_data[stage] <= '0;
            end
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask
endmodule

`default_nettype wire

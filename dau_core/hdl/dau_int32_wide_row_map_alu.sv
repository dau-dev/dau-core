`default_nettype none

// Wide-lane per-row field ALU: SLOTS whole 16-byte quad rows per beat, slot
// s at [128*s +: 128], with each row laid out as {aux1, aux0, value, key}
// (field i at bits [32*i +: 32]). The configured dau_map_alu_pkg transform
// is applied independently to every non-pad row; the reserved ALL-ONES
// 128-bit PAD passes through unchanged. One held-beat output register
// provides elastic valid/ready flow, with positions preserved and
// input_last passed through as output_last.
//
// Config latches on the first beat of each batch. Invalid config drains the
// batch and reports ERR_CONFIG, matching the 64-bit row tile. Whole-row
// beats cannot tear, so there is no ERR_STREAM path. This composed-pipeline
// stage is silent on success; the downstream tile's close-out is
// authoritative.
module dau_int32_wide_row_map_alu
    import dau_map_alu_pkg::*;
#(
    parameter int SLOTS = 4  // quad rows per beat: 1, 2, or 4
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

    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [127:0] PAD_ROW = {128{1'b1}};

    typedef enum logic [1:0] {
        STATE_MAP,     // accept and map one beat of SLOTS rows per cycle
        STATE_DISCARD, // bad config: consume the batch to input_last
        STATE_STATUS   // hold an error close-out until accepted
    } state_t;

    state_t state;

    logic       batch_open;
    logic [2:0] active_op;
    logic [1:0] active_dst;
    logic [1:0] active_src_a;
    logic [1:0] active_src_b;
    logic [4:0] active_shift;

    logic input_fire;
    logic output_fire;
    logic first_beat;
    logic config_ok;

    logic [2:0] eff_op;
    logic [1:0] eff_dst;
    logic [1:0] eff_src_a;
    logic [1:0] eff_src_b;
    logic [4:0] eff_shift;

    logic [SLOTS*128-1:0] mapped_data;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign input_ready = ((state == STATE_MAP) && (!output_valid || output_ready))
        || (state == STATE_DISCARD);

    assign first_beat = !batch_open;
    assign eff_op = first_beat ? cfg_op : active_op;
    assign eff_dst = first_beat ? cfg_dst_field : active_dst;
    assign eff_src_a = first_beat ? cfg_src_a_field : active_src_a;
    assign eff_src_b = first_beat ? cfg_src_b_field : active_src_b;
    assign eff_shift = first_beat ? cfg_shift : active_shift;

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

    function automatic logic [127:0] map_row(input logic [127:0] row);
        logic [31:0] operand_a;
        logic [31:0] operand_b;
        logic signed [31:0] mul_a;
        logic signed [31:0] mul_b;
        logic signed [63:0] product;
        logic [31:0] cheap_mapped;
        logic [31:0] mapped;
        begin
            operand_a = field_select(eff_src_a, row);
            operand_b = field_select(eff_src_b, row);
            mul_a = signed'(operand_a);
            mul_b = signed'(operand_b);
            product = mul_a * mul_b;
            cheap_mapped = map_cheap_result(eff_op, operand_a, operand_b, eff_shift);
            mapped = map_finish_sample(eff_op, product, cheap_mapped, eff_shift);
            map_row = row;
            unique case (eff_dst)
                2'd0: map_row[31:0] = mapped;
                2'd1: map_row[63:32] = mapped;
                2'd2: map_row[95:64] = mapped;
                default: map_row[127:96] = mapped;
            endcase
        end
    endfunction

    always_comb begin
        mapped_data = input_data;
        for (int s = 0; s < SLOTS; s++) begin
            if (input_data[128*s +: 128] != PAD_ROW) begin
                mapped_data[128*s +: 128] = map_row(input_data[128*s +: 128]);
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
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
                            output_valid <= 1'b1;
                            output_data <= mapped_data;
                            output_last <= input_last;
                            if (input_last) begin
                                // silent success: the downstream tile closes out
                                batch_open <= 1'b0;
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
            output_valid <= 1'b0;
            output_last <= 1'b0;
            batch_open <= 1'b0;
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
            active_op <= 3'd0;
            active_dst <= 2'd0;
            active_src_a <= 2'd0;
            active_src_b <= 2'd0;
            active_shift <= 5'd0;
            output_valid <= 1'b0;
            output_data <= '0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask
endmodule

`default_nettype wire

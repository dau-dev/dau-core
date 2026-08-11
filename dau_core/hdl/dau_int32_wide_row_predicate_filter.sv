`default_nettype none

// Wide-lane row-atomic compound predicate filter: SLOTS whole 16-byte quad
// rows per beat, slot s at [128*s +: 128], with each row laid out as
// {aux1, aux0, value, key} (field i at bits [32*i +: 32]). The reserved
// ALL-ONES 128-bit row is the PAD: pads pass unchanged and are neither
// evaluated nor counted. A non-pad row that fails the three-term predicate
// is dropped by replacing its slot with PAD — positions are preserved and
// no compaction occurs. A globally stalled PIPELINE_DEPTH-stage pipeline
// preserves elastic valid/ready flow; input_last travels with its beat.
//
// Predicate config and selected_count semantics match the 64-bit row tile.
// Config latches on the first beat of each batch; invalid config drains the
// batch and reports ERR_CONFIG. Whole-row beats cannot tear, so there is no
// ERR_STREAM path. This composed-pipeline stage is silent on success; even
// an all-dropped batch forwards its final beat as pads, leaving downstream
// responsible for the success close-out.
module dau_int32_wide_row_predicate_filter
    import dau_predicate_pkg::*;
#(
    parameter int SLOTS = 4,          // quad rows per beat: 1, 2, 4, or 8
    parameter int PIPELINE_DEPTH = 3  // elastic stages: 1 through 8
) (
    input  wire logic                         clk,
    input  wire logic                         rst,
    input  wire logic [1:0]                   cfg_field0,
    input  wire logic [2:0]                   cfg_op0,
    input  wire logic [31:0]                  cfg_constant0,
    input  wire logic [1:0]                   cfg_combine01,
    input  wire logic [1:0]                   cfg_field1,
    input  wire logic [2:0]                   cfg_op1,
    input  wire logic [31:0]                  cfg_constant1,
    input  wire logic [1:0]                   cfg_combine2,
    input  wire logic [1:0]                   cfg_field2,
    input  wire logic [2:0]                   cfg_op2,
    input  wire logic [31:0]                  cfg_constant2,
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
    output logic [7:0]                        status_error_code,
    output logic [63:0]                       selected_count
);
    if (!(SLOTS == 1 || SLOTS == 2 || SLOTS == 4 || SLOTS == 8)) begin : g_bad_slots
        $error("SLOTS must be 1, 2, 4, or 8");
    end
    if (PIPELINE_DEPTH < 1 || PIPELINE_DEPTH > 8) begin : g_bad_pipeline_depth
        $error("PIPELINE_DEPTH must be between 1 and 8");
    end

    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [127:0] PAD_ROW = {128{1'b1}};

    typedef enum logic [1:0] {
        STATE_FILTER,  // accept and filter one beat of SLOTS rows per cycle
        STATE_DISCARD, // bad config: consume the batch to input_last
        STATE_STATUS   // hold an error close-out until accepted
    } state_t;

    state_t state;

    logic        batch_open;
    logic        batch_input_closed;
    logic [1:0]  active_field0;
    logic [2:0]  active_op0;
    logic [31:0] active_constant0;
    logic [1:0]  active_combine01;
    logic [1:0]  active_field1;
    logic [2:0]  active_op1;
    logic [31:0] active_constant1;
    logic [1:0]  active_combine2;
    logic [1:0]  active_field2;
    logic [2:0]  active_op2;
    logic [31:0] active_constant2;

    logic advance;
    logic input_fire;
    logic output_fire;
    logic first_beat;
    logic config_ok;
    logic accepted_filter_beat;

    logic [PIPELINE_DEPTH-1:0] pipeline_valid;
    logic [PIPELINE_DEPTH-1:0] pipeline_last;
    logic [SLOTS*128-1:0] pipeline_data [0:PIPELINE_DEPTH-1];
    logic [SLOTS*128-1:0] computed_data;
    logic final_input_valid;
    logic [SLOTS*128-1:0] final_input_data;
    logic [3:0] final_input_selected_count;

    assign advance = !output_valid || output_ready;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign input_ready = advance && (((state == STATE_FILTER) && !batch_input_closed)
        || (state == STATE_DISCARD));

    assign first_beat = !batch_open;
    assign accepted_filter_beat = input_fire && (state == STATE_FILTER)
        && (!first_beat || config_ok);
    assign output_valid = pipeline_valid[PIPELINE_DEPTH-1];
    assign output_data = pipeline_data[PIPELINE_DEPTH-1];
    assign output_last = pipeline_last[PIPELINE_DEPTH-1];

    assign config_ok = predicate_op_valid(cfg_op0)
        && (cfg_combine01 <= COMBINE_OR)
        && (cfg_combine2 <= COMBINE_OR)
        && ((cfg_combine01 == COMBINE_SINGLE) || predicate_op_valid(cfg_op1))
        && ((cfg_combine2 == COMBINE_SINGLE) || predicate_op_valid(cfg_op2));

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

    function automatic logic configured_row_matches(
        input logic [127:0] row,
        input logic [1:0] field0,
        input logic [2:0] op0,
        input logic [31:0] constant0,
        input logic [1:0] combine01,
        input logic [1:0] field1,
        input logic [2:0] op1,
        input logic [31:0] constant1,
        input logic [1:0] combine2,
        input logic [1:0] field2,
        input logic [2:0] op2,
        input logic [31:0] constant2
    );
        logic term0;
        logic term1;
        logic term2;
        logic level01;
        begin
            term0 = predicate_compare(op0, 64'(signed'(field_select(field0, row))), 64'(signed'(constant0)));
            term1 = predicate_compare(op1, 64'(signed'(field_select(field1, row))), 64'(signed'(constant1)));
            term2 = predicate_compare(op2, 64'(signed'(field_select(field2, row))), 64'(signed'(constant2)));
            level01 = (combine01 == COMBINE_AND) ? (term0 && term1)
                : (combine01 == COMBINE_OR) ? (term0 || term1)
                : term0;
            configured_row_matches = (combine2 == COMBINE_AND) ? (level01 && term2)
                : (combine2 == COMBINE_OR) ? (level01 || term2)
                : level01;
        end
    endfunction

    always_comb begin
        computed_data = input_data;
        for (int s = 0; s < SLOTS; s++) begin
            if ((input_data[128*s +: 128] != PAD_ROW)
                && !configured_row_matches(
                    input_data[128*s +: 128],
                    first_beat ? cfg_field0 : active_field0,
                    first_beat ? cfg_op0 : active_op0,
                    first_beat ? cfg_constant0 : active_constant0,
                    first_beat ? cfg_combine01 : active_combine01,
                    first_beat ? cfg_field1 : active_field1,
                    first_beat ? cfg_op1 : active_op1,
                    first_beat ? cfg_constant1 : active_constant1,
                    first_beat ? cfg_combine2 : active_combine2,
                    first_beat ? cfg_field2 : active_field2,
                    first_beat ? cfg_op2 : active_op2,
                    first_beat ? cfg_constant2 : active_constant2
                )) begin
                computed_data[128*s +: 128] = PAD_ROW;
            end
        end
    end

    if (PIPELINE_DEPTH == 1) begin : g_final_from_compute
        assign final_input_valid = accepted_filter_beat;
        assign final_input_data = computed_data;
    end else begin : g_final_from_pipeline
        assign final_input_valid = pipeline_valid[PIPELINE_DEPTH-2];
        assign final_input_data = pipeline_data[PIPELINE_DEPTH-2];
    end

    always_comb begin
        final_input_selected_count = 4'd0;
        for (int s = 0; s < SLOTS; s++) begin
            if (final_input_data[128*s +: 128] != PAD_ROW) begin
                final_input_selected_count = final_input_selected_count + 4'd1;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (input_fire && (state == STATE_FILTER) && first_beat) begin
                selected_count <= 64'd0;
            end
            if (advance) begin
                for (int stage = PIPELINE_DEPTH - 1; stage > 0; stage--) begin
                    pipeline_valid[stage] <= pipeline_valid[stage-1];
                    pipeline_data[stage] <= pipeline_data[stage-1];
                    pipeline_last[stage] <= pipeline_last[stage-1];
                end
                pipeline_valid[0] <= accepted_filter_beat;
                pipeline_data[0] <= computed_data;
                pipeline_last[0] <= input_last;

                if (final_input_valid) begin
                    if (input_fire && (state == STATE_FILTER) && first_beat) begin
                        selected_count <= {{60{1'b0}}, final_input_selected_count};
                    end else begin
                        selected_count <= selected_count + {{60{1'b0}}, final_input_selected_count};
                    end
                end
            end

            if (output_fire && output_last) begin
                batch_open <= 1'b0;
                batch_input_closed <= 1'b0;
            end
            case (state)
                STATE_FILTER: begin
                    if (input_fire) begin
                        if (first_beat) begin
                            batch_open <= 1'b1;
                            active_field0 <= cfg_field0;
                            active_op0 <= cfg_op0;
                            active_constant0 <= cfg_constant0;
                            active_combine01 <= cfg_combine01;
                            active_field1 <= cfg_field1;
                            active_op1 <= cfg_op1;
                            active_constant1 <= cfg_constant1;
                            active_combine2 <= cfg_combine2;
                            active_field2 <= cfg_field2;
                            active_op2 <= cfg_op2;
                            active_constant2 <= cfg_constant2;
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
                        state <= STATE_FILTER;
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
            selected_count <= 64'd0;
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
            state <= STATE_FILTER;
            batch_open <= 1'b0;
            batch_input_closed <= 1'b0;
            active_field0 <= 2'd0;
            active_op0 <= 3'd0;
            active_constant0 <= 32'd0;
            active_combine01 <= 2'd0;
            active_field1 <= 2'd0;
            active_op1 <= 3'd0;
            active_constant1 <= 32'd0;
            active_combine2 <= 2'd0;
            active_field2 <= 2'd0;
            active_op2 <= 3'd0;
            active_constant2 <= 32'd0;
            pipeline_valid <= '0;
            pipeline_last <= '0;
            for (int stage = 0; stage < PIPELINE_DEPTH; stage++) begin
                pipeline_data[stage] <= '0;
            end
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            selected_count <= 64'd0;
        end
    endtask
endmodule

`default_nettype wire

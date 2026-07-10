`default_nettype none

// Keyed compacting predicate filter (composed-pipeline stage).
//
// Consumes one keyed row per 64-bit word ({value[31:0], key[31:0]}) and
// keeps whole rows whose selected lane (cfg_target_lane: 0 = value,
// 1 = key) matches `lane <op0> constant0`, optionally combined with
// `lane <op1> constant1` (AND/OR) — compaction never misaligns the lanes.
// Predicate semantics come from dau_predicate_pkg (the
// dau_core.operators.filter_rows golden).
//
// cfg_row_count = N checks the stream length (STREAM error on mismatch);
// cfg_row_count = 0 selects last-driven mode for stages downstream of
// another compaction, where the count is unknowable at config time — the
// upstream's last marker is trusted. A one-deep pending buffer provides
// output_last lookahead when trailing rows are dropped; if the entire
// stream drops, no output words are emitted (selected_count reports 0).
module dau_int32_keyed_predicate_filter
    import dau_predicate_pkg::*;
(
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        cfg_target_lane,
    input  wire logic [2:0]  cfg_op0,
    input  wire logic [31:0] cfg_constant0,
    input  wire logic [1:0]  cfg_combine,
    input  wire logic [2:0]  cfg_op1,
    input  wire logic [31:0] cfg_constant1,
    input  wire logic [63:0] cfg_row_count,
    input  wire logic        input_valid,
    output logic             input_ready,
    input  wire logic [63:0] input_data,
    input  wire logic        input_last,
    output logic             output_valid,
    input  wire logic        output_ready,
    output logic [63:0]      output_data,
    output logic             output_last,
    output logic             status_valid,
    input  wire logic        status_ready,
    output logic             status_error,
    output logic [7:0]       status_error_code,
    output logic [63:0]      selected_count
);
    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;

    typedef enum logic [2:0] {
        STATE_IDLE,
        STATE_STREAM,
        STATE_DRAIN_ERROR,
        STATE_FINISH,
        STATE_STATUS_PEND,
        STATE_STATUS
    } state_t;

    state_t state;

    logic        active_target_lane;
    logic [2:0]  active_op0;
    logic [31:0] active_constant0;
    logic [1:0]  active_combine;
    logic [2:0]  active_op1;
    logic [31:0] active_constant1;
    logic        count_checked;
    logic [63:0] rows_remaining;
    logic        pending_valid;
    logic [63:0] pending_word;
    logic [7:0]  pending_error_code;

    logic input_fire;
    logic output_fire;
    logic status_fire;
    logic config_ok;
    logic final_word;
    logic row_matches;

    logic        eff_target_lane;
    logic [2:0]  eff_op0;
    logic [31:0] eff_constant0;
    logic [1:0]  eff_combine;
    logic [2:0]  eff_op1;
    logic [31:0] eff_constant1;
    logic [63:0] eff_remaining;
    logic        eff_count_checked;

    assign eff_target_lane = (state == STATE_IDLE) ? cfg_target_lane : active_target_lane;
    assign eff_op0 = (state == STATE_IDLE) ? cfg_op0 : active_op0;
    assign eff_constant0 = (state == STATE_IDLE) ? cfg_constant0 : active_constant0;
    assign eff_combine = (state == STATE_IDLE) ? cfg_combine : active_combine;
    assign eff_op1 = (state == STATE_IDLE) ? cfg_op1 : active_op1;
    assign eff_constant1 = (state == STATE_IDLE) ? cfg_constant1 : active_constant1;
    assign eff_remaining = (state == STATE_IDLE) ? cfg_row_count : rows_remaining;
    assign eff_count_checked = (state == STATE_IDLE) ? (|cfg_row_count) : count_checked;

    assign input_ready = ((state == STATE_IDLE) || (state == STATE_STREAM) || (state == STATE_DRAIN_ERROR))
        && (!output_valid || output_ready) && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    assign config_ok = predicate_op_valid(cfg_op0)
        && (cfg_combine <= COMBINE_OR)
        && ((cfg_combine == COMBINE_SINGLE) || predicate_op_valid(cfg_op1));

    // count-checked mode: registered down-counter, compare-to-constant only.
    // last-driven mode (row_count 0): the upstream last marker decides.
    assign final_word = eff_count_checked ? ((~|eff_remaining[63:1]) && eff_remaining[0]) : input_last;

    assign row_matches = predicate_matches(
        eff_op0, eff_constant0, eff_combine, eff_op1, eff_constant1,
        eff_target_lane ? input_data[31:0] : input_data[63:32]
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_IDLE, STATE_STREAM: begin
                    if (input_fire) begin
                        if ((state == STATE_IDLE) && !config_ok) begin
                            fail(ERR_CONFIG);
                        end else if (eff_count_checked && (input_last != final_word)) begin
                            if (input_last) begin
                                start_error_status(ERR_STREAM);
                            end else begin
                                fail(ERR_STREAM);
                            end
                        end else begin
                            if (state == STATE_IDLE) begin
                                active_target_lane <= cfg_target_lane;
                                active_op0 <= cfg_op0;
                                active_constant0 <= cfg_constant0;
                                active_combine <= cfg_combine;
                                active_op1 <= cfg_op1;
                                active_constant1 <= cfg_constant1;
                                count_checked <= |cfg_row_count;
                            end
                            rows_remaining <= eff_remaining - 64'd1;
                            if (row_matches) begin
                                if (pending_valid) begin
                                    output_valid <= 1'b1;
                                    output_data <= pending_word;
                                    output_last <= 1'b0;
                                end
                                pending_valid <= 1'b1;
                                pending_word <= input_data;
                                selected_count <= selected_count + 64'd1;
                            end
                            if (final_word) begin
                                state <= STATE_FINISH;
                            end else begin
                                state <= STATE_STREAM;
                            end
                        end
                    end
                end
                STATE_DRAIN_ERROR: begin
                    if (input_fire && input_last) begin
                        start_error_status(pending_error_code);
                    end
                end
                STATE_FINISH: begin
                    if (!output_valid || output_ready) begin
                        if (pending_valid) begin
                            output_valid <= 1'b1;
                            output_data <= pending_word;
                            output_last <= 1'b1;
                            pending_valid <= 1'b0;
                            state <= STATE_STATUS_PEND;
                        end else begin
                            status_valid <= 1'b1;
                            status_error <= 1'b0;
                            status_error_code <= 8'd0;
                            state <= STATE_STATUS;
                        end
                    end
                end
                STATE_STATUS_PEND: begin
                    if (!output_valid || output_ready) begin
                        status_valid <= 1'b1;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= STATE_STATUS;
                    end
                end
                STATE_STATUS: begin
                    if (status_fire) begin
                        reset_state();
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    task automatic fail(input logic [7:0] code);
        begin
            pending_error_code <= code;
            if (input_last) begin
                start_error_status(code);
            end else begin
                state <= STATE_DRAIN_ERROR;
            end
        end
    endtask

    task automatic start_error_status(input logic [7:0] code);
        begin
            output_valid <= 1'b0;
            output_last <= 1'b0;
            pending_valid <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_IDLE;
            active_target_lane <= 1'b0;
            active_op0 <= 3'd0;
            active_constant0 <= 32'd0;
            active_combine <= COMBINE_SINGLE;
            active_op1 <= 3'd0;
            active_constant1 <= 32'd0;
            count_checked <= 1'b0;
            rows_remaining <= 64'd0;
            pending_valid <= 1'b0;
            pending_word <= 64'd0;
            pending_error_code <= 8'd0;
            selected_count <= 64'd0;
            output_valid <= 1'b0;
            output_data <= 64'd0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask
endmodule

`default_nettype wire

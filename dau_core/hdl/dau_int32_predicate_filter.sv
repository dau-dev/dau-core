`default_nettype none

// Streaming compacting predicate filter (the Q100 colfilter shape).
//
// Consumes a packed INT32 value stream (two samples per 64-bit word, low word
// older), keeps samples matching `value <op0> constant0` optionally combined
// with `value <op1> constant1` (AND/OR), and emits the matching samples
// repacked two-per-word in input order. `selected_count` reports the kept-row
// count with the final status. Configuration ports must be stable from the
// first input beat of a batch to the last; they are latched at the first beat.
//
// Output words pass through a one-deep pending buffer so the final output
// word carries output_last even when trailing input rows do not match.
module dau_int32_predicate_filter
    import dau_predicate_pkg::*;
(
    input  wire logic        clk,
    input  wire logic        rst,
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

    logic [2:0]  active_op0;
    logic [31:0] active_constant0;
    logic [1:0]  active_combine;
    logic [2:0]  active_op1;
    logic [31:0] active_constant1;
    logic [63:0] rows_remaining;
    logic        residue_valid;
    logic [31:0] residue_value;
    logic        pending_valid;
    logic [63:0] pending_word;
    logic [7:0]  pending_error_code;
    // one-cycle-delayed accumulate: the comparators never chain into the
    // 64-bit selected_count adder
    logic [1:0]  match_count_q;
    logic        match_count_q_valid;

    logic input_fire;
    logic output_fire;
    logic status_fire;
    logic config_ok;
    logic [63:0] rows_this_word;
    logic final_word;
    logic match_low;
    logic match_high;

    // effective config: live ports on the first beat, latched copies after
    logic [2:0]  eff_op0;
    logic [31:0] eff_constant0;
    logic [1:0]  eff_combine;
    logic [2:0]  eff_op1;
    logic [31:0] eff_constant1;
    logic [63:0] eff_remaining;

    assign eff_op0 = (state == STATE_IDLE) ? cfg_op0 : active_op0;
    assign eff_constant0 = (state == STATE_IDLE) ? cfg_constant0 : active_constant0;
    assign eff_combine = (state == STATE_IDLE) ? cfg_combine : active_combine;
    assign eff_op1 = (state == STATE_IDLE) ? cfg_op1 : active_op1;
    assign eff_constant1 = (state == STATE_IDLE) ? cfg_constant1 : active_constant1;
    assign eff_remaining = (state == STATE_IDLE) ? cfg_row_count : rows_remaining;

    assign input_ready = ((state == STATE_IDLE) || (state == STATE_STREAM) || (state == STATE_DRAIN_ERROR))
        && (!output_valid || output_ready) && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    assign config_ok = predicate_op_valid(cfg_op0)
        && (cfg_combine <= COMBINE_OR)
        && ((cfg_combine == COMBINE_SINGLE) || predicate_op_valid(cfg_op1))
        && (cfg_row_count != 64'd0);

    // down-counter with compare-to-constant checks: the 64-bit subtract is a
    // clean register-to-register path, never chained into compares (the wide
    // subtract-then-compare chain misses 8 ns — same lesson as the
    // aggregation payload path)
    assign rows_this_word = (|eff_remaining[63:1]) ? 64'd2 : 64'd1;
    assign final_word = (~|eff_remaining[63:2]) && ((eff_remaining[1:0] == 2'd2) || (eff_remaining[1:0] == 2'd1));
    assign match_low = predicate_matches(eff_op0, eff_constant0, eff_combine, eff_op1, eff_constant1, input_data[31:0]);
    assign match_high = (rows_this_word == 64'd2)
        && predicate_matches(eff_op0, eff_constant0, eff_combine, eff_op1, eff_constant1, input_data[63:32]);

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            unique case (state)
                STATE_IDLE, STATE_STREAM: begin
                    if (input_fire) begin
                        if ((state == STATE_IDLE) && !config_ok) begin
                            fail(ERR_CONFIG);
                        end else if (input_last != final_word) begin
                            // truncated stream or missing last marker
                            if (input_last) begin
                                start_error_status(ERR_STREAM);
                            end else begin
                                fail(ERR_STREAM);
                            end
                        end else begin
                            if (state == STATE_IDLE) begin
                                active_op0 <= cfg_op0;
                                active_constant0 <= cfg_constant0;
                                active_combine <= cfg_combine;
                                active_op1 <= cfg_op1;
                                active_constant1 <= cfg_constant1;
                            end
                            process_word();
                        end
                    end
                end
                STATE_DRAIN_ERROR: begin
                    if (input_fire && input_last) begin
                        start_error_status(pending_error_code);
                    end
                end
                STATE_FINISH: begin
                    flush_match_count();
                    match_count_q_valid <= 1'b0;
                    if (!output_valid || output_ready) begin
                        if (pending_valid) begin
                            output_valid <= 1'b1;
                            output_data <= pending_word;
                            output_last <= !residue_valid;
                            pending_valid <= 1'b0;
                            if (!residue_valid) begin
                                state <= STATE_STATUS_PEND;
                            end
                        end else if (residue_valid) begin
                            output_valid <= 1'b1;
                            output_data <= {32'd0, residue_value};
                            output_last <= 1'b1;
                            residue_valid <= 1'b0;
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

    task automatic process_word;
        logic [1:0] match_count;
        logic [1:0] total;
        logic [31:0] first_sample;
        logic [31:0] second_sample;
        begin
            match_count = {1'b0, match_low} + {1'b0, match_high};
            total = {1'b0, residue_valid} + match_count;
            // ordered kept samples: [residue?, low-match?, high-match?]
            first_sample = residue_valid ? residue_value : (match_low ? input_data[31:0] : input_data[63:32]);
            second_sample = (residue_valid && match_low) ? input_data[31:0] : input_data[63:32];

            match_count_q <= match_count;
            match_count_q_valid <= 1'b1;
            flush_match_count();
            rows_remaining <= eff_remaining - rows_this_word;

            if (total >= 2'd2) begin
                // a full output word forms; route it through the pending buffer
                if (pending_valid) begin
                    output_valid <= 1'b1;
                    output_data <= pending_word;
                    output_last <= 1'b0;
                end
                pending_valid <= 1'b1;
                pending_word <= {second_sample, first_sample};
                residue_valid <= (total == 2'd3);
                residue_value <= input_data[63:32];
            end else if (total == 2'd1) begin
                residue_valid <= 1'b1;
                residue_value <= first_sample;
            end

            if (final_word) begin
                state <= STATE_FINISH;
            end else begin
                state <= STATE_STREAM;
            end
        end
    endtask

    task automatic flush_match_count;
        begin
            if (match_count_q_valid) begin
                selected_count <= selected_count + {62'd0, match_count_q};
            end
        end
    endtask

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
            residue_valid <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_IDLE;
            active_op0 <= 3'd0;
            active_constant0 <= 32'd0;
            active_combine <= COMBINE_SINGLE;
            active_op1 <= 3'd0;
            active_constant1 <= 32'd0;
            rows_remaining <= 64'd0;
            residue_valid <= 1'b0;
            residue_value <= 32'd0;
            pending_valid <= 1'b0;
            pending_word <= 64'd0;
            pending_error_code <= 8'd0;
            match_count_q <= 2'd0;
            match_count_q_valid <= 1'b0;
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

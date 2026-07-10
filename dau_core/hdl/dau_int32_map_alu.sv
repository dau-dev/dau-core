`default_nettype none

// Streaming per-row map/ALU tile (roadmap M2 T2, Q100/map-asbd14 shape).
//
// Consumes a packed INT32 value stream (two samples per 64-bit word, low word
// older) and emits one output word per input word with the configured ALU
// operation applied to each sample: value <op> constant, where MULSHR/SHL
// also use cfg_shift ((value * constant) >>> shift — multiply-shift floor
// division for bucket derivation; value << shift for key packing). All
// arithmetic is INT32 two's-complement wraparound, bit-for-bit the
// dau_core.operators.map_values golden. Configuration ports must be stable
// from the first input beat of a batch to the last; they are latched at the
// first beat.
//
// Each word takes two cycles: multiplier products are registered (DSP
// pipeline register) before the shift/select stage, keeping the 32x32
// multiply off the 8 ns critical path.
module dau_int32_map_alu
    import dau_map_alu_pkg::*;
(
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [2:0]  cfg_op,
    input  wire logic [31:0] cfg_constant,
    input  wire logic [4:0]  cfg_shift,
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
    output logic [7:0]       status_error_code
);
    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;

    typedef enum logic [2:0] {
        STATE_IDLE,
        STATE_STREAM,
        STATE_MUL,
        STATE_EMIT,
        STATE_DRAIN_ERROR,
        STATE_STATUS_PEND,
        STATE_STATUS
    } state_t;

    state_t state;

    logic [2:0]  active_op;
    logic [31:0] active_constant;
    logic [4:0]  active_shift;
    logic [63:0] rows_remaining;
    logic        emit_last;
    logic [7:0]  pending_error_code;

    // stage-1 operand registers feed the multipliers a full clean cycle
    // (DSP48 cascade is register-to-register); stage-2 holds the products
    logic signed [31:0] mul_a_low;
    logic signed [31:0] mul_a_high;
    logic signed [31:0] mul_b;
    logic signed [63:0] product_low;
    logic signed [63:0] product_high;
    logic [31:0] cheap_low;
    logic [31:0] cheap_high;

    logic input_fire;
    logic output_fire;
    logic status_fire;
    logic config_ok;
    logic [63:0] rows_this_word;
    logic final_word;

    logic [2:0]  eff_op;
    logic [31:0] eff_constant;
    logic [4:0]  eff_shift;
    logic [63:0] eff_remaining;

    assign eff_op = (state == STATE_IDLE) ? cfg_op : active_op;
    assign eff_constant = (state == STATE_IDLE) ? cfg_constant : active_constant;
    assign eff_shift = (state == STATE_IDLE) ? cfg_shift : active_shift;
    assign eff_remaining = (state == STATE_IDLE) ? cfg_row_count : rows_remaining;

    assign input_ready = ((state == STATE_IDLE) || (state == STATE_STREAM) || (state == STATE_DRAIN_ERROR))
        && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    // all non-zero 3-bit opcodes are defined (ADD..OR); zero is reserved
    assign config_ok = (cfg_op != 3'd0) && (cfg_row_count != 64'd0);

    // down-counter with compare-to-constant checks: the 64-bit subtract is a
    // clean register-to-register path, never chained into compares (the wide
    // subtract-then-compare chain misses 8 ns — same lesson as the
    // aggregation payload path)
    assign rows_this_word = (|eff_remaining[63:1]) ? 64'd2 : 64'd1;
    assign final_word = (~|eff_remaining[63:2]) && ((eff_remaining[1:0] == 2'd2) || (eff_remaining[1:0] == 2'd1));

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
                            if (input_last) begin
                                start_error_status(ERR_STREAM);
                            end else begin
                                fail(ERR_STREAM);
                            end
                        end else begin
                            if (state == STATE_IDLE) begin
                                active_op <= cfg_op;
                                active_constant <= cfg_constant;
                                active_shift <= cfg_shift;
                            end
                            mul_a_low <= signed'(input_data[31:0]);
                            mul_a_high <= signed'(input_data[63:32]);
                            mul_b <= signed'(eff_constant);
                            cheap_low <= map_cheap_result(eff_op, input_data[31:0], eff_constant, eff_shift);
                            cheap_high <= (rows_this_word == 64'd2) ? map_cheap_result(eff_op, input_data[63:32], eff_constant, eff_shift) : 32'd0;
                            rows_remaining <= eff_remaining - rows_this_word;
                            emit_last <= final_word;
                            state <= STATE_MUL;
                        end
                    end
                end
                STATE_MUL: begin
                    product_low <= mul_a_low * mul_b;
                    product_high <= mul_a_high * mul_b;
                    state <= STATE_EMIT;
                end
                STATE_EMIT: begin
                    output_valid <= 1'b1;
                    output_data <= {map_finish_sample(active_op, product_high, cheap_high, active_shift), map_finish_sample(active_op, product_low, cheap_low, active_shift)};
                    output_last <= emit_last;
                    if (emit_last) begin
                        state <= STATE_STATUS_PEND;
                    end else begin
                        state <= STATE_STREAM;
                    end
                end
                STATE_DRAIN_ERROR: begin
                    if (input_fire && input_last) begin
                        start_error_status(pending_error_code);
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
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_IDLE;
            active_op <= 3'd0;
            active_constant <= 32'd0;
            active_shift <= 5'd0;
            rows_remaining <= 64'd0;
            emit_last <= 1'b0;
            pending_error_code <= 8'd0;
            mul_a_low <= 32'd0;
            mul_a_high <= 32'd0;
            mul_b <= 32'd0;
            product_low <= 64'd0;
            product_high <= 64'd0;
            cheap_low <= 32'd0;
            cheap_high <= 32'd0;
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

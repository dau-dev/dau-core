`default_nettype none

// Per-row field ALU for 16-byte quad rows (two 64-bit words,
// w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th 32-bit lane in
// (key, value, aux0, aux1) order). Each row's configured destination field
// is rewritten as dst = src_a <op> src_b over two selected field operands
// (dau_map_alu_pkg semantics — INT32 wraparound, MULSHR's 64-bit product
// arithmetic-shifted right; SHL ignores src_b); the other fields pass
// through unchanged, one row out per row in, row count preserved
// (dau_core.operators.map_row_fields is the golden). The fixed-point
// revenue derivation value = value * aux0 >>> shift is this tile's q6 job.
// The multiply runs register-to-register across a dedicated stage, keeping
// the DSP cascade off the 8 ns critical path.
//
// A composed-pipeline stage: last-driven only (the upstream compaction's
// last marker is trusted), silent on success — the downstream tile's
// close-out is authoritative. It raises its own status only for an invalid
// config (cfg_op zero/unknown, checked at the first row of a batch and
// latched for the batch: the batch drains, then ERR_CONFIG) and for a torn
// final row (input_last on a w0: ERR_STREAM).
module dau_int32_row_map_alu
    import dau_map_alu_pkg::*;
(
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [2:0]  cfg_op,
    input  wire logic [1:0]  cfg_dst_field,
    input  wire logic [1:0]  cfg_src_a_field,
    input  wire logic [1:0]  cfg_src_b_field,
    input  wire logic [4:0]  cfg_shift,
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
        STATE_W0,      // accept w0 of a row (batch config latches on the first)
        STATE_W1,      // accept w1 of a row, capture the operands
        STATE_MUL,     // registered product stage
        STATE_EMIT_W0, // emit the mapped row's first word
        STATE_EMIT_W1, // emit the mapped row's second word
        STATE_DISCARD, // bad config: consume the batch to input_last
        STATE_STATUS   // hold an error close-out until accepted
    } state_t;

    state_t state;

    logic        batch_open;
    logic [2:0]  active_op;
    logic [1:0]  active_dst;
    logic [1:0]  active_src_a;
    logic [1:0]  active_src_b;
    logic [4:0]  active_shift;

    logic [63:0] row_w0;
    logic [63:0] row_w1;
    logic        row_last;

    logic signed [31:0] mul_a;
    logic signed [31:0] mul_b;
    logic signed [63:0] product;
    logic [31:0] cheap_mapped;

    logic input_fire;
    logic output_fire;
    logic config_ok;
    logic first_row;

    logic [2:0]  eff_op;
    logic [1:0]  eff_src_a;
    logic [1:0]  eff_src_b;
    logic [4:0]  eff_shift;
    logic [31:0] operand_a;
    logic [31:0] operand_b;
    logic [31:0] mapped;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;

    // no acceptance while a word is pending on the output: an error raised
    // against a later beat must never have to retract an asserted valid
    assign input_ready = ((state == STATE_W0) || (state == STATE_W1) || (state == STATE_DISCARD)) && !output_valid && !status_valid;

    assign first_row = !batch_open;
    assign eff_op = first_row ? cfg_op : active_op;
    assign eff_src_a = first_row ? cfg_src_a_field : active_src_a;
    assign eff_src_b = first_row ? cfg_src_b_field : active_src_b;
    assign eff_shift = first_row ? cfg_shift : active_shift;

    assign config_ok = cfg_op != 3'd0;

    function automatic logic [31:0] field_select(input logic [1:0] sel, input logic [63:0] w0, input logic [63:0] w1);
        begin
            unique case (sel)
                2'd0: field_select = w0[31:0];
                2'd1: field_select = w0[63:32];
                2'd2: field_select = w1[31:0];
                default: field_select = w1[63:32];
            endcase
        end
    endfunction

    assign operand_a = field_select(eff_src_a, row_w0, input_data);
    assign operand_b = field_select(eff_src_b, row_w0, input_data);
    assign mapped = map_finish_sample(active_op, product, cheap_mapped, active_shift);

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_W0: begin
                    if (input_fire) begin
                        if (first_row) begin
                            batch_open <= 1'b1;
                            active_op <= cfg_op;
                            active_dst <= cfg_dst_field;
                            active_src_a <= cfg_src_a_field;
                            active_src_b <= cfg_src_b_field;
                            active_shift <= cfg_shift;
                        end
                        if (first_row && !config_ok) begin
                            // config precedence over stream shape: drain the
                            // batch, then CONFIG
                            if (input_last) begin
                                raise_error(ERR_CONFIG);
                            end else begin
                                state <= STATE_DISCARD;
                            end
                        end else if (input_last) begin
                            // torn row: the batch is invalid
                            raise_error(ERR_STREAM);
                        end else begin
                            row_w0 <= input_data;
                            state <= STATE_W1;
                        end
                    end
                end
                STATE_W1: begin
                    if (input_fire) begin
                        row_w1 <= input_data;
                        row_last <= input_last;
                        mul_a <= signed'(operand_a);
                        mul_b <= signed'(operand_b);
                        cheap_mapped <= map_cheap_result(eff_op, operand_a, operand_b, eff_shift);
                        state <= STATE_MUL;
                    end
                end
                STATE_MUL: begin
                    product <= mul_a * mul_b;
                    state <= STATE_EMIT_W0;
                end
                STATE_EMIT_W0: begin
                    // wait for the previous row's trailing word to drain
                    if (!output_valid) begin
                        output_valid <= 1'b1;
                        output_last <= 1'b0;
                        unique case (active_dst)
                            2'd0: output_data <= {row_w0[63:32], mapped};
                            2'd1: output_data <= {mapped, row_w0[31:0]};
                            default: output_data <= row_w0;
                        endcase
                        state <= STATE_EMIT_W1;
                    end
                end
                STATE_EMIT_W1: begin
                    if (!output_valid) begin
                        output_valid <= 1'b1;
                        output_last <= row_last;
                        unique case (active_dst)
                            2'd2: output_data <= {row_w1[63:32], mapped};
                            2'd3: output_data <= {mapped, row_w1[31:0]};
                            default: output_data <= row_w1;
                        endcase
                        if (row_last) begin
                            // silent success: the downstream tile closes out
                            batch_open <= 1'b0;
                        end
                        state <= STATE_W0;
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
                        state <= STATE_W0;
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
            state <= STATE_W0;
            batch_open <= 1'b0;
            active_op <= 3'd0;
            active_dst <= 2'd0;
            active_src_a <= 2'd0;
            active_src_b <= 2'd0;
            active_shift <= 5'd0;
            row_w0 <= 64'd0;
            row_w1 <= 64'd0;
            row_last <= 1'b0;
            mul_a <= 32'd0;
            mul_b <= 32'd0;
            product <= 64'd0;
            cheap_mapped <= 32'd0;
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

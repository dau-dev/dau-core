`default_nettype none

`include "types.svh"

// Per-row field ALU for 16-byte quad rows (two 64-bit words,
// w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th FIELD_WIDTH lane,
// which at the default int32 is (key, value, aux0, aux1) order). Each row's configured destination field
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
module dau_row_map_alu
    import dau_map_alu_pkg::*;
#(
    // The ELEMENT TYPE of one field, as encoded by types.svh. The 128-bit
    // quad row is the transport unit and stays fixed, so a narrower type
    // packs MORE fields per row rather than shrinking the beat. A plain
    // signed integer encodes as its own width, so `DAU_TYPE_INT32 is 32 and
    // this reproduces the four-field row the tile has always had.
    parameter int FIELD_WIDTH = `DAU_TYPE_INT32
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [2:0]  cfg_op,
    input  wire logic [SEL_BITS-1:0] cfg_dst_field,
    input  wire logic [SEL_BITS-1:0] cfg_src_a_field,
    input  wire logic [SEL_BITS-1:0] cfg_src_b_field,
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

    localparam int ROW_BITS = 128;                 // the transport unit, fixed
    // the type, taken apart: the data lanes size off FIELD_BITS and the
    // remaining three facts are what the guards check the type against
    localparam int FIELD_BITS      = `DAU_TYPE_WIDTH(FIELD_WIDTH);
    localparam int FIELD_FRAC_BITS = `DAU_TYPE_FRAC_BITS(FIELD_WIDTH);
    localparam int FIELD_IS_FLOAT  = `DAU_TYPE_IS_FLOAT(FIELD_WIDTH);
    localparam int FIELD_IS_SIGNED = `DAU_TYPE_IS_SIGNED(FIELD_WIDTH);
    localparam int FIELDS      = ROW_BITS / FIELD_BITS;
    localparam int WORD_FIELDS = 64 / FIELD_BITS;  // fields carried by one stream word
    // at most 32 bits per field puts at least two fields in a word, so
    // FIELDS is exactly 2*WORD_FIELDS and SEL_BITS is WORD_SEL_BITS + 1
    localparam int WORD_SEL_BITS = $clog2(WORD_FIELDS);
    localparam int SEL_BITS    = (FIELDS <= 1) ? 1 : $clog2(FIELDS);

    // The upper bound is MAP_ELEMENT_BITS, which the ALU package derives from
    // its product path rather than asserting -- see dau_map_alu_pkg.
    if (FIELD_BITS < 8 || FIELD_BITS > MAP_ELEMENT_BITS || (ROW_BITS % FIELD_BITS) != 0) begin : g_field_width_check
        $error("FIELD_WIDTH must divide 128 and lie in [8, 32]");
    end
    // A TYPE IS NOT A CAPABILITY. This datapath multiplies operands as
    // two's-complement magnitudes and finishes MULSHR with an ARITHMETIC
    // right shift: handed a float it would multiply exponent bits, and handed
    // an unsigned type it would read every high-bit-set value as negative and
    // shift a borrowed sign in. Both are silently wrong answers.
    if (FIELD_IS_FLOAT != 0) begin : g_field_float_check
        $error("FIELD_WIDTH must not name a floating-point type: this tile is an integer ALU");
    end
    if (FIELD_IS_SIGNED == 0) begin : g_field_signed_check
        $error("FIELD_WIDTH must not name an unsigned type: this tile multiplies and shifts two's-complement integers");
    end
    // FIXED POINT IS ADMISSIBLE and is what MULSHR exists for: the shift the
    // host configures is the format's fractional width. The point must still
    // fit inside the field.
    if (FIELD_FRAC_BITS >= FIELD_BITS) begin : g_field_fraction_check
        $error("FIELD_WIDTH names a fixed-point type whose fraction fills the whole field");
    end

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
    logic [SEL_BITS-1:0] active_dst;
    logic [SEL_BITS-1:0] active_src_a;
    logic [SEL_BITS-1:0] active_src_b;
    logic [4:0]  active_shift;

    logic [63:0] row_w0;
    logic [63:0] row_w1;
    logic        row_last;

    logic signed [FIELD_BITS-1:0]   mul_a;
    logic signed [FIELD_BITS-1:0]   mul_b;
    logic signed [2*FIELD_BITS-1:0] product;
    logic [FIELD_BITS-1:0] cheap_mapped;

    logic input_fire;
    logic output_fire;
    logic config_ok;
    logic first_row;

    logic [2:0]  eff_op;
    logic [SEL_BITS-1:0] eff_src_a;
    logic [SEL_BITS-1:0] eff_src_b;
    logic [4:0]  eff_shift;
    logic [FIELD_BITS-1:0] operand_a;
    logic [FIELD_BITS-1:0] operand_b;
    logic [FIELD_BITS-1:0] mapped;

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

    // field i is the i-th lane of the quad row {w1, w0}: lanes 0..WORD_FIELDS-1
    // ride the first stream word, the rest the second.
    function automatic logic [FIELD_BITS-1:0] field_select(input logic [SEL_BITS-1:0] sel, input logic [63:0] w0, input logic [63:0] w1);
        logic [ROW_BITS-1:0] row;
        begin
            row = {w1, w0};
            field_select = row[sel*FIELD_BITS +: FIELD_BITS];
        end
    endfunction

    // the mapped result placed back into one stream word. A destination is
    // decoded from its BITS -- the top selector bit picks the word, the rest
    // the lane inside it -- rather than compared arithmetically against a
    // lane base, which measures a LUT dearer for the same function.
    function automatic logic [63:0] place_field(
        input logic [63:0] word,
        input logic [SEL_BITS-1:0] dst,
        input logic [FIELD_BITS-1:0] value,
        input logic word_index
    );
        begin
            place_field = word;
            if (dst[SEL_BITS-1] == word_index) begin
                place_field[dst[WORD_SEL_BITS-1:0]*FIELD_BITS +: FIELD_BITS] = value;
            end
        end
    endfunction

    assign operand_a = field_select(eff_src_a, row_w0, input_data);
    assign operand_b = field_select(eff_src_b, row_w0, input_data);
    // the package's ALU is written at MAP_ELEMENT_BITS; a narrower field rides
    // it sign-extended and comes back truncated (dau_map_alu_pkg states why
    // that is the same arithmetic one width down)
    assign mapped = FIELD_BITS'(map_finish_sample(active_op, MAP_PRODUCT_BITS'(signed'(product)), MAP_ELEMENT_BITS'(signed'(cheap_mapped)), active_shift));

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
                        cheap_mapped <= FIELD_BITS'(map_cheap_result(eff_op, MAP_ELEMENT_BITS'(signed'(operand_a)), MAP_ELEMENT_BITS'(signed'(operand_b)), eff_shift));
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
                        output_data <= place_field(row_w0, active_dst, mapped, 1'b0);
                        state <= STATE_EMIT_W1;
                    end
                end
                STATE_EMIT_W1: begin
                    if (!output_valid) begin
                        output_valid <= 1'b1;
                        output_last <= row_last;
                        output_data <= place_field(row_w1, active_dst, mapped, 1'b1);
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
            active_dst <= '0;
            active_src_a <= '0;
            active_src_b <= '0;
            active_shift <= 5'd0;
            row_w0 <= 64'd0;
            row_w1 <= 64'd0;
            row_last <= 1'b0;
            mul_a <= '0;
            mul_b <= '0;
            product <= '0;
            cheap_mapped <= '0;
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

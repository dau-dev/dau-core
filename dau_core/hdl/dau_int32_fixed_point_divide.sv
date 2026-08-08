`default_nettype none

// Fixed-point divide over 16-byte quad rows (two 64-bit words,
// w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th 32-bit lane in
// (key, value, aux0, aux1) order). The configured destination field is
// rewritten as numerator / denominator in Q(32-F).F fixed point; the other
// fields pass through unchanged, one row out per row in, row count preserved
// (dau_core.operators.divide_row_fields is the golden).
//
// This is what retires the deferred-division convention. Means, VWAP and
// z-score denominators were accumulated as exact integers and divided on the
// host because the platform had no divider.
//
// EXACT. With a = A * 2**-F and b = B * 2**-F, the quotient (a/b) * 2**F is
// (A << F) / B -- an integer division, so a restoring long division produces
// the golden bit for bit and the bench compares with no tolerance.
//
// FLOOR, not truncation toward zero: MULSHR already divides by powers of two
// with an arithmetic shift, and -7 >> 1 is -4. A tile that truncated toward
// zero would disagree with the map ALU on every negative operand. The
// recurrence produces a magnitude, so the floor correction is applied at
// retire -- negative and inexact rounds away from zero.
//
// One iteration per pipeline stage, so the critical path is a single
// REM_BITS compare-and-subtract whatever the format width. The quotient
// shifts into the dividend register as it vacates, which is why there is one
// ITERATIONS-wide register here and not two.
//
// A composed-pipeline stage: last-driven only, silent on success -- the
// downstream tile's close-out is authoritative. dst may be either source,
// updating in place. Three of its own statuses, all raised at INGEST so
// there is one drain path:
//   ERR_STREAM  a torn final row (input_last on a w0)
//   ERR_DOMAIN  a zero divisor, which has no quotient
//   ERR_RANGE   a quotient that is defined but does not fit int32. Fixed
//               point overflows easily -- 1.0 / (1 * 2**-16) is 2**32 -- and
//               the check is exact WITHOUT dividing: the quotient fits iff
//               |A| << F is below |B| << 31 (at or below it when the signs
//               differ, where the result rounds the other way). Wrapping or
//               saturating instead would be a wrong number that looks right.
module dau_int32_fixed_point_divide #(
    // fractional bits of the fixed-point format (16 = Q16.16)
    parameter int FRACTIONAL_BITS = 16
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [1:0]  cfg_dst_field,
    input  wire logic [1:0]  cfg_numerator_field,
    input  wire logic [1:0]  cfg_denominator_field,
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
    localparam logic [7:0] ERR_STREAM = 8'd2;
    localparam logic [7:0] ERR_DOMAIN = 8'd3;
    localparam logic [7:0] ERR_RANGE  = 8'd4;

    // |A| reaches 2**31 (for A = -2**31), so the dividend is 32 + F bits and
    // the quotient is the same width before the range check narrows it
    localparam int ITERATIONS = 32 + FRACTIONAL_BITS;
    // the remainder stays below the divisor (<= 2**31) and is shifted once
    // more before the compare
    localparam int REM_BITS = 33;
    // |B| << 31 is the widest term: 32 magnitude bits shifted 31
    localparam int LIMIT_BITS = 32 + 31;

    if (FRACTIONAL_BITS < 0 || FRACTIONAL_BITS > 31) begin : g_fractional_bits_check
        $error("FRACTIONAL_BITS must be in [0, 31]");
    end

    typedef enum logic [1:0] {
        IN_W0,
        IN_W1,
        IN_DISCARD
    } in_state_t;

    typedef enum logic [1:0] {
        OUT_IDLE,
        OUT_W0,
        OUT_W1
    } out_state_t;

    in_state_t in_state;
    out_state_t out_state;

    logic        batch_open;
    logic [1:0]  active_dst;
    logic [1:0]  active_num;
    logic [1:0]  active_den;
    logic        status_pending;
    logic [7:0]  pending_error;

    logic [31:0] held_field0;
    logic [31:0] held_field1;

    logic                 stage_valid [ITERATIONS+1];
    logic [REM_BITS-1:0]  stage_rem   [ITERATIONS+1];
    // the dividend, progressively replaced by the quotient it vacates
    logic [ITERATIONS-1:0] stage_qd   [ITERATIONS+1];
    logic [31:0]          stage_div   [ITERATIONS+1];
    logic [31:0]          stage_field [ITERATIONS+1][4];
    logic [1:0]           stage_dst   [ITERATIONS+1];
    logic                 stage_neg   [ITERATIONS+1];
    logic                 stage_last  [ITERATIONS+1];

    logic [31:0] emit_field [4];
    logic        emit_last;

    logic input_fire;
    logic output_fire;
    logic retire_valid;
    logic pipe_advance;
    logic pipe_push;

    logic [31:0] in_field [4];
    logic [31:0] num_value;
    logic [31:0] den_value;
    logic [31:0] num_magnitude;
    logic [31:0] den_magnitude;
    logic        result_negative;
    logic        divisor_zero;
    logic        quotient_overflows;
    logic [LIMIT_BITS-1:0] scaled_numerator;
    logic [LIMIT_BITS-1:0] overflow_limit;

    assign in_field[0] = held_field0;
    assign in_field[1] = held_field1;
    assign in_field[2] = input_data[31:0];
    assign in_field[3] = input_data[63:32];
    assign num_value = in_field[active_num];
    assign den_value = in_field[active_den];

    assign num_magnitude = num_value[31] ? (~num_value + 32'd1) : num_value;
    assign den_magnitude = den_value[31] ? (~den_value + 32'd1) : den_value;
    assign result_negative = num_value[31] ^ den_value[31];
    assign divisor_zero = (den_value == 32'd0);

    // exact overflow test, no division: |A| << F against |B| << 31. At the
    // limit the same-sign result is 2**31 (one too large) while the
    // opposite-sign result is exactly -2**31 (representable), so the
    // comparison is inclusive only when the signs agree.
    assign scaled_numerator = {{(LIMIT_BITS - 32 - FRACTIONAL_BITS){1'b0}}, num_magnitude, {FRACTIONAL_BITS{1'b0}}};
    assign overflow_limit = {den_magnitude, 31'd0};
    assign quotient_overflows = result_negative ? (scaled_numerator > overflow_limit) : (scaled_numerator >= overflow_limit);

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign retire_valid = stage_valid[ITERATIONS];

    assign pipe_advance = !retire_valid || (out_state == OUT_IDLE);

    assign input_ready = !status_pending && ((in_state == IN_W0) || (in_state == IN_DISCARD) || ((in_state == IN_W1) && pipe_advance));

    // No separate zero-divisor term: a zero divisor makes the overflow limit
    // zero, so the comparison below is already true for every numerator --
    // dividing by zero is infinite, which certainly does not fit. The DOMAIN
    // code still exists to name the cause in the close-out.
    assign pipe_push = (in_state == IN_W1) && input_fire && !quotient_overflows;

    assign output_valid = (out_state == OUT_W0) || (out_state == OUT_W1);
    assign output_data = (out_state == OUT_W0) ? {emit_field[1], emit_field[0]} : {emit_field[3], emit_field[2]};
    assign output_last = (out_state == OUT_W1) && emit_last;

    assign status_valid = status_pending;
    assign status_error = 1'b1;
    assign status_error_code = pending_error;

    always_ff @(posedge clk) begin
        if (rst) begin
            in_state <= IN_W0;
            out_state <= OUT_IDLE;
            batch_open <= 1'b0;
            status_pending <= 1'b0;
            pending_error <= 8'd0;
            emit_last <= 1'b0;
            for (int unsigned i = 0; i <= ITERATIONS; i++) begin
                stage_valid[i] <= 1'b0;
            end
        end else begin
            if (pipe_advance) begin
                stage_valid[0] <= pipe_push;
                if (pipe_push) begin
                    stage_rem[0] <= '0;
                    stage_qd[0] <= {num_magnitude, {FRACTIONAL_BITS{1'b0}}};
                    stage_div[0] <= den_magnitude;
                    stage_neg[0] <= result_negative;
                    stage_last[0] <= input_last;
                    stage_dst[0] <= active_dst;
                    for (int unsigned f = 0; f < 4; f++) begin
                        stage_field[0][f] <= in_field[f];
                    end
                end

                for (int unsigned i = 0; i < ITERATIONS; i++) begin
                    automatic logic [REM_BITS-1:0] shifted;
                    automatic logic               fits;
                    shifted = {stage_rem[i][REM_BITS-2:0], stage_qd[i][ITERATIONS-1]};
                    fits = (shifted >= {1'b0, stage_div[i]});

                    stage_valid[i+1] <= stage_valid[i];
                    stage_last[i+1] <= stage_last[i];
                    stage_dst[i+1] <= stage_dst[i];
                    stage_neg[i+1] <= stage_neg[i];
                    stage_div[i+1] <= stage_div[i];
                    for (int unsigned f = 0; f < 4; f++) begin
                        stage_field[i+1][f] <= stage_field[i][f];
                    end
                    // the quotient bit shifts in where the consumed dividend
                    // bit shifted out
                    stage_qd[i+1] <= {stage_qd[i][ITERATIONS-2:0], fits};
                    stage_rem[i+1] <= fits ? (shifted - {1'b0, stage_div[i]}) : shifted;
                end
            end

            case (in_state)
                IN_W0: begin
                    if (input_fire) begin
                        if (input_last) begin
                            pending_error <= ERR_STREAM;
                            status_pending <= 1'b1;
                        end else begin
                            held_field0 <= input_data[31:0];
                            held_field1 <= input_data[63:32];
                            if (!batch_open) begin
                                batch_open <= 1'b1;
                                active_dst <= cfg_dst_field;
                                active_num <= cfg_numerator_field;
                                active_den <= cfg_denominator_field;
                            end
                            in_state <= IN_W1;
                        end
                    end
                end

                IN_W1: begin
                    if (input_fire) begin
                        if (divisor_zero || quotient_overflows) begin
                            pending_error <= divisor_zero ? ERR_DOMAIN : ERR_RANGE;
                            if (input_last) status_pending <= 1'b1;
                            else in_state <= IN_DISCARD;
                        end else begin
                            in_state <= IN_W0;
                            if (input_last) batch_open <= 1'b0;
                        end
                    end
                end

                IN_DISCARD: begin
                    if (input_fire && input_last) begin
                        status_pending <= 1'b1;
                    end
                end

                default: in_state <= IN_W0;
            endcase

            case (out_state)
                OUT_IDLE: begin
                    if (retire_valid) begin
                        automatic logic [31:0] magnitude;
                        automatic logic [31:0] signed_result;
                        magnitude = stage_qd[ITERATIONS][31:0];
                        // floor: a negative quotient with a remainder rounds
                        // away from zero, which is one further down
                        signed_result = stage_neg[ITERATIONS]
                            ? ((stage_rem[ITERATIONS] != '0) ? (~magnitude) : (~magnitude + 32'd1))
                            : magnitude;
                        for (int unsigned f = 0; f < 4; f++) begin
                            emit_field[f] <= stage_field[ITERATIONS][f];
                        end
                        emit_field[stage_dst[ITERATIONS]] <= signed_result;
                        emit_last <= stage_last[ITERATIONS];
                        out_state <= OUT_W0;
                    end
                end

                OUT_W0: if (output_fire) out_state <= OUT_W1;

                OUT_W1: if (output_fire) out_state <= OUT_IDLE;

                default: out_state <= OUT_IDLE;
            endcase

            if (status_pending && status_ready) begin
                status_pending <= 1'b0;
                in_state <= IN_W0;
                batch_open <= 1'b0;
                for (int unsigned i = 0; i <= ITERATIONS; i++) begin
                    stage_valid[i] <= 1'b0;
                end
            end
        end
    end
endmodule

`default_nettype wire

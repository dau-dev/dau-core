`default_nettype none

`include "types.svh"

// Per-symbol rolling-moment tile (the roll stage of the temporal workflow,
// over the reduced bar/feature stream). Over a count-based window of the last
// WINDOW values per symbol, emits the deferred-division moment record
// (count, sum, sum-of-squares) for every FULL window — one tile serving
// rolling sum (=sum), mean (=sum/count) and sample variance
// (=(count*sum_sq - sum*sum)/(count*(count-1))), the host dividing once.
// Matches dau_core.temporal.rolling_moments/rolling_sum (warm-up rows, where
// the window is not yet full, are dropped — the None entries the golden emits).
//
// The input is one host-sorted, symbol-grouped stream, one value per
// row (w0 = {value, symbol_id[31:0]}, the value in the word's upper lane at
// VALUE_WIDTH; input_last on
// the word ends the stream). The SYMBOL ID is a 32-bit container, not an
// element -- it identifies a series rather than measuring anything, and the
// rolling extremum tile, which alternates with this one in a composition,
// fixes it the same way. Because the stream is symbol-grouped, exactly one
// symbol is live at a time, so the tile holds ONE window (a depth-WINDOW shift
// register) reset on symbol change — the bar tile's key-change shape, no
// per-symbol table. sum/sum_sq are INT64, which is what bounds VALUE_WIDTH
// from above.
//
// ARITHMETIC PRECONDITION, and at int32 it binds INSIDE the declared domain:
// sum_sq accumulates WINDOW squares, so the host must guarantee
//     WINDOW * max(|value|)**2 <= 2**63 - 1
// Full-range int32 does NOT satisfy this. (2**31-1)**2 is already 2**61, so
// the sum of squares needs 64 bits at WINDOW=3 and 68 at WINDOW=64 -- it
// wraps SILENTLY and returns a wrong variance rather than an error.
//
// The largest magnitude each window admits at int32:
//     WINDOW   2 -> |v| <= 2,147,483,647   (full int32, the only safe window)
//     WINDOW   4 -> |v| <= 1,518,500,249
//     WINDOW   8 -> |v| <= 1,073,741,823
//     WINDOW  64 -> |v| <= 379,625,062
//     WINDOW 256 -> |v| <= 189,812,531
// The tile cannot check this -- it sees one row at a time and has no bound to
// compare against -- so the obligation is DECLARED in three places that can:
// the registry entry states it, dau_core.temporal.rolling_moments REFUSES a
// window whose sum of squares leaves INT64 (so the golden can never quietly
// disagree with this datapath), and the lowering proves it ahead of time --
// dau-polars refuses a rolling scan whose value column declares no
// `magnitude_bits`, and checks WINDOW against the declared bound when it does.
//
// NARROWING THE ELEMENT WEAKENS THE PRECONDITION, which is the one direction
// it can move: at int16 the whole declared domain is safe to WINDOW = 2**33,
// so the obligation becomes vacuous. Widening would make it worse, and the
// guard below refuses it.
//
// The moment update runs in four beats (READ -> MUL -> DIFF -> UPDATE) so no
// register-to-register path carries more than one operation: READ latches the
// entering/leaving values (behind the symbol-change select), MUL forms the two
// 32x32 DSP products from registered inputs, DIFF differences them into the
// sum-of-squares increment, UPDATE folds the increments into sum/sum_sq with a
// single add each. A row is accepted every fourth cycle (the reduced bar stream
// is small; throughput is not the constraint). The effective prior accumulator
// is read from the committed total in UPDATE (no row overlaps in the pipeline),
// zeroed on a symbol change, so only control bits ride the stages.
//
// A full window emits a 3-word record:
//   r0 = {count[31:0], symbol_id[31:0]}   (count == WINDOW)
//   r1 = sum[63:0]        (INT64)
//   r2 = sum_sq[63:0]     (INT64)
// One record is buffered so the final emitted record carries output_last even
// when the stream ends on a warm-up or symbol-change row (the row-stream
// contract). WINDOW >= 2 (a size-1 sample variance is undefined; the compiler
// residues it).
module dau_rolling_moments #(
    parameter int WINDOW = 4,
    // The ELEMENT TYPE of the value lane, as encoded by types.svh -- what a
    // value IS, from which its width follows. The symbol id beside it is a
    // 32-bit container and does not move. `DAU_TYPE_INT32 is 32, so the
    // default reads exactly the {value, symbol} word the tile has always read.
    parameter int VALUE_WIDTH = `DAU_TYPE_INT32
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        input_valid,
    output logic             input_ready,
    // the symbol id and, above it, the value. At the default int32 those fill
    // the word; a narrower element leaves the bits above the value reserved
    // and unread (the streaming dedup says the same about its row).
    /* verilator lint_off UNUSEDSIGNAL */
    input  wire logic [63:0] input_data,
    /* verilator lint_on UNUSEDSIGNAL */
    input  wire logic        input_last,
    output logic             output_valid,
    input  wire logic        output_ready,
    output logic [63:0]      output_data,
    output logic             output_last,
    output logic             status_valid,
    input  wire logic        status_ready,
    output logic             status_error,
    output logic [7:0]       status_error_code,
    output logic [63:0]      moment_count
);
    localparam int CW = $clog2(WINDOW + 1);
    localparam int SYMBOL_BITS = 32;  // the series id: a container, not an element
    localparam int VALUE_BITS      = `DAU_TYPE_WIDTH(VALUE_WIDTH);
    localparam int VALUE_IS_FLOAT  = `DAU_TYPE_IS_FLOAT(VALUE_WIDTH);
    localparam int VALUE_IS_SIGNED = `DAU_TYPE_IS_SIGNED(VALUE_WIDTH);

    if (WINDOW < 2) begin : g_param_check
        $error("WINDOW must be >= 2 (a size-1 sample variance is undefined)");
    end
    // The upper bound is sum_sq: 32 is where a squared value still leaves the
    // INT64 accumulator any window at all (see the precondition above), and a
    // wider element would need a wider record, i.e. a different tile. The
    // lower bound is the palette's narrowest element.
    if (VALUE_BITS < 8 || VALUE_BITS > 32) begin : g_value_width_check
        $error("VALUE_WIDTH must be in [8, 32]");
    end
    // A TYPE IS NOT A CAPABILITY. The datapath sums the value and its square
    // into two's-complement INT64 accumulators through a signed DSP multiply:
    // handed a float it would multiply exponent bits, and handed an unsigned
    // type it would read every high-bit-set value as negative.
    if (VALUE_IS_FLOAT != 0) begin : g_value_float_check
        $error("VALUE_WIDTH must not name a floating-point type: this tile sums two's-complement integers and their squares");
    end
    if (VALUE_IS_SIGNED == 0) begin : g_value_signed_check
        $error("VALUE_WIDTH must not name an unsigned type: this tile sign-extends into signed accumulators");
    end

    typedef enum logic [2:0] {
        STATE_READ,
        STATE_MUL,
        STATE_DIFF,
        STATE_UPDATE,
        STATE_EMIT_R0,
        STATE_EMIT_R1,
        STATE_EMIT_R2,
        STATE_STATUS
    } state_t;

    state_t state;

    logic               group_valid;
    logic signed [SYMBOL_BITS-1:0] cur_symbol;
    logic signed [VALUE_BITS-1:0] win [WINDOW];
    logic [CW-1:0]      filled;
    logic signed [63:0] total;
    logic signed [63:0] total_sq;

    // control bits + values riding the four-beat pipeline (wide accumulators are
    // NOT carried — UPDATE reads the committed total)
    // stage 1 (READ)
    logic signed [SYMBOL_BITS-1:0] s_symbol;
    logic               s_last;
    logic               s_new_group;
    logic               s_window_full;
    logic [CW-1:0]      s_eff_filled;
    logic signed [63:0] s_delta;
    logic signed [VALUE_BITS-1:0] s_value;
    logic signed [VALUE_BITS-1:0] s_departing;
    // stage 2 (MUL)
    logic signed [SYMBOL_BITS-1:0] t_symbol;
    logic               t_last;
    logic               t_new_group;
    logic               t_window_full;
    logic [CW-1:0]      t_eff_filled;
    logic signed [63:0] t_delta;
    logic signed [63:0] t_val_sq;
    logic signed [63:0] t_dep_sq;
    // stage 3 (DIFF)
    logic signed [SYMBOL_BITS-1:0] u_symbol;
    logic               u_last;
    logic               u_new_group;
    logic               u_window_full;
    logic [CW-1:0]      u_eff_filled;
    logic signed [63:0] u_delta;
    logic signed [63:0] u_delta_sq;

    // the moment record being emitted, and the one-row look-ahead buffer
    logic signed [SYMBOL_BITS-1:0] emit_symbol;
    logic signed [63:0] emit_total;
    logic signed [63:0] emit_total_sq;
    logic               emit_final;
    logic               buf_valid;
    logic signed [SYMBOL_BITS-1:0] buf_symbol;
    logic signed [63:0] buf_total;
    logic signed [63:0] buf_total_sq;
    logic               post_emit_flush;

    logic input_fire;
    logic output_fire;
    logic status_fire;

    logic signed [SYMBOL_BITS-1:0] row_symbol;
    logic signed [VALUE_BITS-1:0] row_value;
    logic               new_group;
    logic [CW-1:0]      eff_filled;
    logic               window_full;
    logic signed [VALUE_BITS-1:0] departing;

    // UPDATE-beat fold: effective prior accumulator (committed total, zeroed on a
    // symbol change) plus the registered increments
    logic signed [63:0] eff_total;
    logic signed [63:0] eff_total_sq;
    logic signed [63:0] new_total;
    logic signed [63:0] new_total_sq;
    logic               warm;

    assign input_ready = (state == STATE_READ) && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    assign row_symbol = signed'(input_data[0 +: SYMBOL_BITS]);
    assign row_value = signed'(input_data[SYMBOL_BITS +: VALUE_BITS]);

    // effective prior window at READ: empty when this row opens a new group
    assign new_group = !group_valid || (row_symbol != cur_symbol);
    assign eff_filled = new_group ? '0 : filled;
    assign window_full = (eff_filled == WINDOW[CW-1:0]);
    // the value leaving the window when it is already full; nothing departs
    // while the window is still filling
    assign departing = window_full ? win[WINDOW-1] : '0;

    assign eff_total = u_new_group ? 64'sd0 : total;
    assign eff_total_sq = u_new_group ? 64'sd0 : total_sq;
    assign new_total = eff_total + u_delta;
    assign new_total_sq = eff_total_sq + u_delta_sq;
    // full after this insert: already full, or this insert fills it
    assign warm = u_window_full || (u_eff_filled == WINDOW[CW-1:0] - 1'b1);

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_READ: begin
                    if (input_fire) begin
                        // intake beat: shift the value in and latch the entering
                        // and leaving values (behind the symbol-change select)
                        push_value();
                        group_valid <= 1'b1;
                        cur_symbol <= row_symbol;
                        s_symbol <= row_symbol;
                        s_last <= input_last;
                        s_new_group <= new_group;
                        s_window_full <= window_full;
                        s_eff_filled <= eff_filled;
                        s_delta <= 64'(row_value) - 64'(departing);
                        s_value <= row_value;
                        s_departing <= departing;
                        state <= STATE_MUL;
                    end
                end
                STATE_MUL: begin
                    // product beat: 32x32 DSP products from registered inputs
                    t_symbol <= s_symbol;
                    t_last <= s_last;
                    t_new_group <= s_new_group;
                    t_window_full <= s_window_full;
                    t_eff_filled <= s_eff_filled;
                    t_delta <= s_delta;
                    t_val_sq <= s_value * s_value;
                    t_dep_sq <= s_departing * s_departing;
                    state <= STATE_DIFF;
                end
                STATE_DIFF: begin
                    // difference beat: the sum-of-squares increment
                    u_symbol <= t_symbol;
                    u_last <= t_last;
                    u_new_group <= t_new_group;
                    u_window_full <= t_window_full;
                    u_eff_filled <= t_eff_filled;
                    u_delta <= t_delta;
                    u_delta_sq <= t_val_sq - t_dep_sq;
                    state <= STATE_UPDATE;
                end
                STATE_UPDATE: begin
                    // update beat: fold the increments into the moment
                    total <= new_total;
                    total_sq <= new_total_sq;
                    filled <= u_window_full ? WINDOW[CW-1:0] : (u_eff_filled + 1'b1);
                    if (warm) begin
                        accept_moment(u_last);
                    end else begin
                        close_or_next(u_last);
                    end
                end
                STATE_EMIT_R0: begin
                    if (!output_valid || output_ready) begin
                        emit_word({WINDOW[31:0], emit_symbol}, 1'b0, STATE_EMIT_R1);
                    end
                end
                STATE_EMIT_R1: begin
                    if (!output_valid || output_ready) begin
                        emit_word(unsigned'(emit_total), 1'b0, STATE_EMIT_R2);
                    end
                end
                STATE_EMIT_R2: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= unsigned'(emit_total_sq);
                        output_last <= emit_final;
                        moment_count <= moment_count + 64'd1;
                        if (emit_final) begin
                            enter_status();
                        end else if (post_emit_flush) begin
                            load_emit_from_buffer();
                            emit_final <= 1'b1;
                            buf_valid <= 1'b0;
                            post_emit_flush <= 1'b0;
                            state <= STATE_EMIT_R0;
                        end else begin
                            state <= STATE_READ;
                        end
                    end
                end
                STATE_STATUS: begin
                    // close-out: drain the last output beat, raise success, wait
                    if (!status_valid) begin
                        if (!output_valid || output_ready) begin
                            status_valid <= 1'b1;
                            status_error <= 1'b0;
                            status_error_code <= 8'd0;
                        end
                    end else if (status_fire) begin
                        reset_stream_state();
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    // shift the new value into win[0]; stale entries beyond `filled` are never
    // read (departing is consulted only once the window is genuinely full)
    task automatic push_value;
        begin
            for (int i = WINDOW - 1; i > 0; i--) begin
                win[i] <= win[i-1];
            end
            win[0] <= row_value;
        end
    endtask

    // a full-window moment. Buffered so the last emitted record can be final:
    // a previously buffered record flushes now (non-final); at stream end the
    // buffer flushes final.
    task automatic accept_moment(input logic last_word);
        begin
            if (buf_valid) begin
                load_emit_from_buffer();
                emit_final <= 1'b0;
                set_buffer(u_symbol, new_total, new_total_sq);
                post_emit_flush <= last_word;
                state <= STATE_EMIT_R0;
            end else if (last_word) begin
                emit_symbol <= u_symbol;
                emit_total <= new_total;
                emit_total_sq <= new_total_sq;
                emit_final <= 1'b1;
                state <= STATE_EMIT_R0;
            end else begin
                set_buffer(u_symbol, new_total, new_total_sq);
                buf_valid <= 1'b1;
                state <= STATE_READ;
            end
        end
    endtask

    // a warm-up / non-emitting row. At stream end flush any buffered record.
    task automatic close_or_next(input logic last_word);
        begin
            if (last_word && buf_valid) begin
                load_emit_from_buffer();
                emit_final <= 1'b1;
                buf_valid <= 1'b0;
                post_emit_flush <= 1'b0;
                state <= STATE_EMIT_R0;
            end else if (last_word) begin
                enter_status();
            end else begin
                state <= STATE_READ;
            end
        end
    endtask

    task automatic enter_status;
        begin
            state <= STATE_STATUS;
        end
    endtask

    task automatic set_buffer(
        input logic signed [SYMBOL_BITS-1:0] symbol,
        input logic signed [63:0] sum,
        input logic signed [63:0] sum_sq
    );
        begin
            buf_symbol <= symbol;
            buf_total <= sum;
            buf_total_sq <= sum_sq;
        end
    endtask

    task automatic load_emit_from_buffer;
        begin
            emit_symbol <= buf_symbol;
            emit_total <= buf_total;
            emit_total_sq <= buf_total_sq;
        end
    endtask

    task automatic emit_word(input logic [63:0] value, input logic last_out, input state_t next_state);
        begin
            output_valid <= 1'b1;
            output_data <= value;
            output_last <= last_out;
            state <= next_state;
        end
    endtask

    task automatic reset_stream_state;
        begin
            state <= STATE_READ;
            group_valid <= 1'b0;
            filled <= '0;
            total <= 64'sd0;
            total_sq <= 64'sd0;
            buf_valid <= 1'b0;
            emit_final <= 1'b0;
            post_emit_flush <= 1'b0;
            moment_count <= 64'd0;
            output_valid <= 1'b0;
            output_data <= 64'd0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask

    task automatic reset_state;
        begin
            reset_stream_state();
            cur_symbol <= '0;
            for (int i = 0; i < WINDOW; i++) begin
                win[i] <= '0;
            end
            s_symbol <= '0;
            s_last <= 1'b0;
            s_new_group <= 1'b0;
            s_window_full <= 1'b0;
            s_eff_filled <= '0;
            s_delta <= 64'sd0;
            s_value <= '0;
            s_departing <= '0;
            t_symbol <= '0;
            t_last <= 1'b0;
            t_new_group <= 1'b0;
            t_window_full <= 1'b0;
            t_eff_filled <= '0;
            t_delta <= 64'sd0;
            t_val_sq <= 64'sd0;
            t_dep_sq <= 64'sd0;
            u_symbol <= '0;
            u_last <= 1'b0;
            u_new_group <= 1'b0;
            u_window_full <= 1'b0;
            u_eff_filled <= '0;
            u_delta <= 64'sd0;
            u_delta_sq <= 64'sd0;
            emit_symbol <= '0;
            emit_total <= 64'sd0;
            emit_total_sq <= 64'sd0;
            buf_symbol <= '0;
            buf_total <= 64'sd0;
            buf_total_sq <= 64'sd0;
        end
    endtask
endmodule

`default_nettype wire

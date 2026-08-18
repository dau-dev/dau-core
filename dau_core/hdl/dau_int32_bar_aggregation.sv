`default_nettype none

`include "types.svh"

// Fused single-pass bar-aggregation tile (ordered close-out): price OHLC
// statistics plus the micro-share volume decomposition in one pass.
//
// Consumes two 64-bit words per row and reads the first four FIELD_WIDTH
// lanes of the ASSEMBLED 128-bit row {w1, w0} — (key, price, shares, frac) —
// which at the default int32 is exactly:
//   w0 = {price[31:0], key[31:0]}
//   w1 = {frac[31:0], shares[31:0]}
// (a row is atomic — input_last is only legal on a w1).
// A bar closes when the key changes or the stream ends; close-out emits a
// 6-word record whose slots do NOT move with the element type — an element
// statistic rides a 32-bit slot sign-extended, an accumulator a 64-bit word:
//   r0 = {open, key}
//   r1 = {low, close}
//   r2 = {32'd0, high}
//   r3 = shares_sum[63:0]   (INT64)
//   r4 = frac_sum[63:0]     (INT64)
//   r5 = count[63:0]
//
// Reappearing keys open new partial bars (the host merges — the
// partial-bar contract). cfg_row_count = N checks the stream length in
// rows; 0 selects last-driven mode. Configuration latches at the first
// beat; matches dau_core.operators.bar_aggregate_rows bit-for-bit.
module dau_int32_bar_aggregation #(
    // The ELEMENT TYPE of one lane, as encoded by types.svh. The 128-bit row
    // is the transport unit and stays fixed; lane i is its i-th FIELD_BITS
    // lane, the decode row-unpack packs and the quad-row filter family reads.
    // `DAU_TYPE_INT32 is 32, so the default reproduces the
    // (key, price, shares, frac) row port for port. The record's element slots
    // are 32 bits and its accumulators 64, which is why the type may narrow
    // but not widen.
    parameter int FIELD_WIDTH = `DAU_TYPE_INT32
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [1:0]  cfg_mode,
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
    output logic [63:0]      bar_count
);
    localparam logic [1:0] MODE_ORDERED = 2'd0;

    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;

    localparam int ROW_BITS = 128;  // the transport unit, fixed
    localparam int FIELDS   = 4;    // the lanes this tile reads
    localparam int FIELD_BITS      = `DAU_TYPE_WIDTH(FIELD_WIDTH);
    localparam int FIELD_IS_FLOAT  = `DAU_TYPE_IS_FLOAT(FIELD_WIDTH);
    localparam int FIELD_IS_SIGNED = `DAU_TYPE_IS_SIGNED(FIELD_WIDTH);
    localparam int LANE_BITS       = FIELDS * FIELD_BITS;       // the part of the row this tile reads
    localparam int DATA_BITS       = 3 * FIELD_BITS;            // its data lanes, key excluded
    localparam int SLOT_BITS       = 32;  // the record's element slot, fixed with the record

    // The upper bound is the RECORD, not the lane: open/high/low/close ride
    // 32-bit slots and the volume sums 64-bit words, so a wider element would
    // need a versioned record, i.e. a different tile. Narrowing is free.
    if (FIELD_BITS < 8 || FIELD_BITS > 32 || (ROW_BITS % FIELD_BITS) != 0) begin : g_field_width_check
        $error("FIELD_WIDTH must divide 128 and lie in [8, 32]");
    end
    // A TYPE IS NOT A CAPABILITY. This datapath ORDERS prices (high/low) and
    // sign-extends volumes into two's-complement INT64 accumulators: handed a
    // float it would order exponent bits as magnitude, and handed an unsigned
    // type it would read every high-bit-set value as negative.
    if (FIELD_IS_FLOAT != 0) begin : g_field_float_check
        $error("FIELD_WIDTH must not name a floating-point type: this tile orders and sums two's-complement integers");
    end
    if (FIELD_IS_SIGNED == 0) begin : g_field_signed_check
        $error("FIELD_WIDTH must not name an unsigned type: this tile orders and sums two's-complement integers");
    end

    typedef enum logic [3:0] {
        STATE_IDLE,
        STATE_ROW_LOW,
        STATE_ROW_HIGH,
        STATE_PENDING_HIGH,
        STATE_EMIT_R0,
        STATE_EMIT_R1,
        STATE_EMIT_R2,
        STATE_EMIT_R3,
        STATE_EMIT_R4,
        STATE_EMIT_R5,
        STATE_DRAIN_ERROR,
        STATE_STATUS_PEND,
        STATE_STATUS
    } state_t;

    state_t state;

    logic        count_checked;
    logic [63:0] rows_remaining;
    logic        bar_open;
    logic signed [FIELD_BITS-1:0] bar_key;
    logic signed [FIELD_BITS-1:0] bar_price_open;
    logic signed [FIELD_BITS-1:0] bar_price_close;
    logic signed [FIELD_BITS-1:0] bar_price_low;
    logic signed [FIELD_BITS-1:0] bar_price_high;
    logic signed [63:0] bar_shares;
    logic signed [63:0] bar_frac;
    logic [63:0] bar_rows;
    logic        final_after_emit;
    // w0 is held WHOLE rather than as its two decoded lanes: at a narrower
    // element the later lanes sit inside w0 too, so the decode has to run on
    // the assembled row and not on either word alone.
    logic [63:0] row_w0;
    logic        held_is_pending;
    logic [7:0]  pending_error_code;

    logic input_fire;
    logic output_fire;
    logic status_fire;
    logic config_ok;
    logic final_row;
    logic key_change;

    logic [63:0] eff_remaining;
    logic        eff_count_checked;
    logic signed [FIELD_BITS-1:0] row_key;
    logic [LANE_BITS-1:0] row_lanes;

    // the row counter loads at the first beat (w0) and decrements at each
    // w1 — final_row is only consulted in the w1 states, so no IDLE mux
    assign eff_remaining = rows_remaining;
    assign eff_count_checked = count_checked;

    assign input_ready = ((state == STATE_IDLE) || (state == STATE_ROW_LOW) || (state == STATE_ROW_HIGH)
        || (state == STATE_PENDING_HIGH) || (state == STATE_DRAIN_ERROR))
        && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    assign config_ok = cfg_mode == MODE_ORDERED;

    // one ROW per two words: the down-counter decrements at w1
    assign final_row = eff_count_checked ? ((~|eff_remaining[63:1]) && eff_remaining[0]) : input_last;

    // lane 0 is always inside w0, so the key compare reads the incoming word
    // directly; the price and volume lanes come from the assembled row
    assign row_key = signed'(input_data[0*FIELD_BITS +: FIELD_BITS]);
    assign row_lanes = LANE_BITS'({input_data, row_w0});
    assign key_change = bar_open && (row_key != bar_key);

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_IDLE, STATE_ROW_LOW: begin
                    if (input_fire) begin
                        if ((state == STATE_IDLE) && !config_ok) begin
                            fail(ERR_CONFIG);
                        end else if (input_last) begin
                            // a row is two words: last on w0 truncates the row
                            start_error_status(ERR_STREAM);
                        end else begin
                            if (state == STATE_IDLE) begin
                                count_checked <= |cfg_row_count;
                                rows_remaining <= cfg_row_count;
                            end
                            row_w0 <= input_data;
                            if (key_change) begin
                                // close the live bar; this row opens the next
                                // bar once its w1 arrives after the emit
                                held_is_pending <= 1'b1;
                                final_after_emit <= 1'b0;
                                state <= STATE_EMIT_R0;
                            end else begin
                                state <= STATE_ROW_HIGH;
                            end
                        end
                    end
                end
                STATE_ROW_HIGH, STATE_PENDING_HIGH: begin
                    if (input_fire) begin
                        if (eff_count_checked && (input_last != final_row)) begin
                            if (input_last) begin
                                start_error_status(ERR_STREAM);
                            end else begin
                                fail(ERR_STREAM);
                            end
                        end else begin
                            rows_remaining <= eff_remaining - 64'd1;
                            if ((state == STATE_PENDING_HIGH) || !bar_open) begin
                                open_bar(row_lanes);
                            end else begin
                                absorb_row(row_lanes[1*FIELD_BITS +: DATA_BITS]);
                            end
                            if (final_row) begin
                                final_after_emit <= 1'b1;
                                state <= STATE_EMIT_R0;
                            end else begin
                                state <= STATE_ROW_LOW;
                            end
                        end
                    end
                end
                STATE_EMIT_R0: begin
                    if (!output_valid || output_ready) begin
                        emit_word(stat_pair(bar_price_open, bar_key), 1'b0, STATE_EMIT_R1);
                    end
                end
                STATE_EMIT_R1: begin
                    if (!output_valid || output_ready) begin
                        emit_word(stat_pair(bar_price_low, bar_price_close), 1'b0, STATE_EMIT_R2);
                    end
                end
                STATE_EMIT_R2: begin
                    if (!output_valid || output_ready) begin
                        emit_word({32'd0, SLOT_BITS'(signed'(bar_price_high))}, 1'b0, STATE_EMIT_R3);
                    end
                end
                STATE_EMIT_R3: begin
                    if (!output_valid || output_ready) begin
                        emit_word(unsigned'(bar_shares), 1'b0, STATE_EMIT_R4);
                    end
                end
                STATE_EMIT_R4: begin
                    if (!output_valid || output_ready) begin
                        emit_word(unsigned'(bar_frac), 1'b0, STATE_EMIT_R5);
                    end
                end
                STATE_EMIT_R5: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= bar_rows;
                        output_last <= final_after_emit && !held_is_pending;
                        bar_count <= bar_count + 64'd1;
                        bar_open <= 1'b0;
                        if (held_is_pending) begin
                            held_is_pending <= 1'b0;
                            state <= STATE_PENDING_HIGH;
                        end else begin
                            state <= STATE_STATUS_PEND;
                        end
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
                        reset_stream_state();
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    task automatic emit_word(input logic [63:0] value, input logic last_word, input state_t next_state);
        begin
            output_valid <= 1'b1;
            output_data <= value;
            output_last <= last_word;
            state <= next_state;
        end
    endtask

    // the record's element slot: one statistic, sign-extended into 32 bits,
    // so a 6-word record decodes the same way at every element type
    function automatic logic [63:0] stat_pair(input logic signed [FIELD_BITS-1:0] hi, input logic signed [FIELD_BITS-1:0] lo);
        stat_pair = {SLOT_BITS'(signed'(hi)), SLOT_BITS'(signed'(lo))};
    endfunction

    task automatic open_bar(input logic [LANE_BITS-1:0] lanes);
        logic signed [FIELD_BITS-1:0] price;
        begin
            price = signed'(lanes[1*FIELD_BITS +: FIELD_BITS]);
            bar_open <= 1'b1;
            bar_key <= signed'(lanes[0*FIELD_BITS +: FIELD_BITS]);
            bar_price_open <= price;
            bar_price_close <= price;
            bar_price_low <= price;
            bar_price_high <= price;
            bar_shares <= 64'(signed'(lanes[2*FIELD_BITS +: FIELD_BITS]));
            bar_frac <= 64'(signed'(lanes[3*FIELD_BITS +: FIELD_BITS]));
            bar_rows <= 64'd1;
        end
    endtask

    task automatic absorb_row(input logic [DATA_BITS-1:0] data_lanes);
        logic signed [FIELD_BITS-1:0] price;
        begin
            price = signed'(data_lanes[0*FIELD_BITS +: FIELD_BITS]);
            bar_price_close <= price;
            if (price < bar_price_low) begin
                bar_price_low <= price;
            end
            if (price > bar_price_high) begin
                bar_price_high <= price;
            end
            bar_shares <= bar_shares + 64'(signed'(data_lanes[1*FIELD_BITS +: FIELD_BITS]));
            bar_frac <= bar_frac + 64'(signed'(data_lanes[2*FIELD_BITS +: FIELD_BITS]));
            bar_rows <= bar_rows + 64'd1;
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
            held_is_pending <= 1'b0;
            bar_open <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_stream_state;
        begin
            state <= STATE_IDLE;
            count_checked <= 1'b0;
            rows_remaining <= 64'd0;
            bar_open <= 1'b0;
            final_after_emit <= 1'b0;
            held_is_pending <= 1'b0;
            pending_error_code <= 8'd0;
            bar_count <= 64'd0;
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
            bar_key <= '0;
            bar_price_open <= '0;
            bar_price_close <= '0;
            bar_price_low <= '0;
            bar_price_high <= '0;
            bar_shares <= 64'd0;
            bar_frac <= 64'd0;
            bar_rows <= 64'd0;
            row_w0 <= 64'd0;
        end
    endtask
endmodule

`default_nettype wire

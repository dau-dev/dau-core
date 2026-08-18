`default_nettype none

// Generic keyed backward as-of join tile. Attaches to each PROBE record the
// most recent BUILD record for the same key with build_time <= probe_time
// (optionally within a tolerance on the time axis) — one held build of bounded
// state, one pass. The payload fields are opaque; the temporal-finance backward
// as-of is this join with quote=build {bid, ask}, trade=probe {price, size},
// key=symbol (that vocabulary lives in the host merge, not the tile).
//
// The two source streams are host-merged into ONE stream sorted by (key, time),
// build-before-probe at equal (key, time). Because the stream is key-grouped,
// exactly ONE key is live at a time: the held build resets when the key changes
// (the same key-change shape the bar tile uses), so no per-key table is needed.
// This is the merge precondition the sorted-scan tiles already require.
//
// Each input record is three 64-bit words (a record is atomic; input_last is
// only legal on w2):
//   w0 = {rec_type[31:0], key[31:0]}         rec_type bit0: 0=build, 1=probe
//   w1 = time[63:0]                          (signed INT64, the merge axis)
//   w2 = {payload_b[31:0], payload_a[31:0]}  build: a/b carried; probe: a/b out
//
// A matched probe emits a 4-word joined row (misses are dropped, like a filter
// — the tile realizes matched joins only):
//   r0 = time[63:0]
//   r1 = {probe_a[31:0], key[31:0]}
//   r2 = {build_b[31:0], build_a[31:0]}
//   r3 = {32'd0, probe_b[31:0]}
//
// cfg_has_tolerance selects the bounded mode: a probe whose held build is older
// than cfg_tolerance (probe_time - build_time > tolerance) is a miss. Config is
// read directly and must be held stable for the job. Matches
// dau_core.operators.keyed_asof_join_rows bit-for-bit.
module dau_asof_backward (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        cfg_has_tolerance,
    input  wire logic [63:0] cfg_tolerance,
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
    output logic [63:0]      match_count
);
    localparam logic [7:0] ERR_STREAM = 8'd2;

    typedef enum logic [3:0] {
        STATE_W0,
        STATE_W1,
        STATE_W2,
        STATE_EMIT_R0,
        STATE_EMIT_R1,
        STATE_EMIT_R2,
        STATE_EMIT_R3,
        STATE_DRAIN_ERROR,
        STATE_STATUS_PEND,
        STATE_STATUS
    } state_t;

    state_t state;

    // held record fields (captured across the three input words)
    logic               rec_is_trade;
    logic signed [31:0] rec_symbol;
    logic signed [63:0] rec_time;

    // held current quote for the live symbol group
    logic               group_valid;
    logic signed [31:0] cur_symbol;
    logic               quote_valid;
    logic signed [63:0] quote_time;
    logic signed [31:0] quote_bid;
    logic signed [31:0] quote_ask;

    // the matched market row being emitted
    logic signed [31:0] emit_symbol;
    logic signed [63:0] emit_time;
    logic signed [31:0] emit_price;
    logic signed [31:0] emit_size;
    logic signed [31:0] emit_bid;
    logic signed [31:0] emit_ask;
    logic               emit_final;         // this row carries output_last

    // one matched row is buffered so the LAST emitted row can be marked final:
    // a match is held until a newer match arrives (it flushes non-final) or the
    // stream ends (it flushes final). Bounded look-ahead, one row.
    logic               buf_valid;
    logic signed [31:0] buf_symbol;
    logic signed [63:0] buf_time;
    logic signed [31:0] buf_price;
    logic signed [31:0] buf_size;
    logic signed [31:0] buf_bid;
    logic signed [31:0] buf_ask;
    logic               post_emit_flush;    // after this emit, flush the buffer as the final row

    logic input_fire;
    logic output_fire;
    logic status_fire;

    logic signed [31:0] beat_lo;    // the two int32 halves of the current beat
    logic signed [31:0] beat_hi;

    logic               symbol_change;
    logic signed [64:0] quote_age;
    logic               within_tolerance;
    logic               trade_matches;

    assign input_ready = ((state == STATE_W0) || (state == STATE_W1) || (state == STATE_W2)
        || (state == STATE_DRAIN_ERROR))
        && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    assign beat_lo = signed'(input_data[31:0]);
    assign beat_hi = signed'(input_data[63:32]);

    // trade for the live symbol: does the held quote qualify? the age is the
    // difference of two signed int64 timestamps, which needs 65 bits — a 64-bit
    // subtraction wraps for legal extreme timestamps and would pass a bounded
    // tolerance it should miss. cfg_tolerance is a non-negative bound.
    assign symbol_change = group_valid && (rec_symbol != cur_symbol);
    assign quote_age = {rec_time[63], rec_time} - {quote_time[63], quote_time};
    assign within_tolerance = !cfg_has_tolerance || (quote_age <= signed'({1'b0, cfg_tolerance}));
    assign trade_matches = !symbol_change && quote_valid && within_tolerance;

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
                        if (input_last) begin
                            // last on w0: a torn record (a record is 3 words)
                            start_error_status(ERR_STREAM);
                        end else begin
                            rec_symbol <= beat_lo;
                            rec_is_trade <= input_data[32];  // rec_type bit0
                            state <= STATE_W1;
                        end
                    end
                end
                STATE_W1: begin
                    if (input_fire) begin
                        if (input_last) begin
                            start_error_status(ERR_STREAM);
                        end else begin
                            rec_time <= signed'(input_data);
                            state <= STATE_W2;
                        end
                    end
                end
                STATE_W2: begin
                    if (input_fire) begin
                        process_record(beat_lo, beat_hi, input_last);
                    end
                end
                STATE_EMIT_R0: begin
                    if (!output_valid || output_ready) begin
                        emit_word(unsigned'(emit_time), 1'b0, STATE_EMIT_R1);
                    end
                end
                STATE_EMIT_R1: begin
                    if (!output_valid || output_ready) begin
                        emit_word({emit_price, emit_symbol}, 1'b0, STATE_EMIT_R2);
                    end
                end
                STATE_EMIT_R2: begin
                    if (!output_valid || output_ready) begin
                        emit_word({emit_ask, emit_bid}, 1'b0, STATE_EMIT_R3);
                    end
                end
                STATE_EMIT_R3: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= {32'd0, emit_size};
                        output_last <= emit_final;
                        match_count <= match_count + 64'd1;
                        if (emit_final) begin
                            state <= STATE_STATUS_PEND;
                        end else if (post_emit_flush) begin
                            // that was the older row; flush the buffered newest
                            // one as the final (last-flagged) output row
                            load_emit_from_buffer();
                            emit_final <= 1'b1;
                            buf_valid <= 1'b0;
                            post_emit_flush <= 1'b0;
                            state <= STATE_EMIT_R0;
                        end else begin
                            state <= STATE_W0;  // a newer match may still arrive
                        end
                    end
                end
                STATE_DRAIN_ERROR: begin
                    if (input_fire && input_last) begin
                        start_error_status(ERR_STREAM);
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

    // Fold the completed record (w2 just arrived) into the held quote or emit a
    // matched market row. `last_word` marks the final record of the stream.
    task automatic process_record(
        input logic signed [31:0] field_a,
        input logic signed [31:0] field_b,
        input logic               last_word
    );
        begin
            if (symbol_change || !group_valid) begin
                // a new symbol group opens; the held quote does not carry over
                group_valid <= 1'b1;
                cur_symbol <= rec_symbol;
                if (rec_is_trade) begin
                    quote_valid <= 1'b0;             // trade before any quote: a miss
                    close_or_next(last_word);
                end else begin
                    quote_valid <= 1'b1;             // the group's first quote
                    quote_time <= rec_time;
                    quote_bid <= field_a;
                    quote_ask <= field_b;
                    close_or_next(last_word);
                end
            end else if (rec_is_trade) begin
                if (trade_matches) begin
                    accept_match(field_a, field_b, last_word);
                end else begin
                    close_or_next(last_word);        // stale / no quote: a miss
                end
            end else begin
                // a newer quote for the live symbol: advance the held quote
                quote_valid <= 1'b1;
                quote_time <= rec_time;
                quote_bid <= field_a;
                quote_ask <= field_b;
                close_or_next(last_word);
            end
        end
    endtask

    // A matched trade. The row is buffered; a previously buffered row flushes
    // now (non-final, a newer match exists). At stream end the buffer flushes
    // as the final row so the last output beat carries output_last.
    task automatic accept_match(
        input logic signed [31:0] price,
        input logic signed [31:0] size,
        input logic               last_word
    );
        begin
            if (buf_valid) begin
                load_emit_from_buffer();
                emit_final <= 1'b0;
                set_buffer(rec_symbol, rec_time, price, size, quote_bid, quote_ask);
                post_emit_flush <= last_word;   // the new buffer flushes final after
                state <= STATE_EMIT_R0;
            end else if (last_word) begin
                emit_symbol <= rec_symbol;
                emit_time <= rec_time;
                emit_price <= price;
                emit_size <= size;
                emit_bid <= quote_bid;
                emit_ask <= quote_ask;
                emit_final <= 1'b1;             // the only pending row at stream end
                state <= STATE_EMIT_R0;
            end else begin
                set_buffer(rec_symbol, rec_time, price, size, quote_bid, quote_ask);
                buf_valid <= 1'b1;
                state <= STATE_W0;
            end
        end
    endtask

    // A non-matching record (a miss or a quote update). At stream end, flush any
    // buffered matched row as the final output row; otherwise keep reading.
    task automatic close_or_next(input logic last_word);
        begin
            if (last_word && buf_valid) begin
                load_emit_from_buffer();
                emit_final <= 1'b1;
                buf_valid <= 1'b0;
                post_emit_flush <= 1'b0;
                state <= STATE_EMIT_R0;
            end else if (last_word) begin
                state <= STATE_STATUS_PEND;
            end else begin
                state <= STATE_W0;
            end
        end
    endtask

    task automatic set_buffer(
        input logic signed [31:0] symbol,
        input logic signed [63:0] tm,
        input logic signed [31:0] price,
        input logic signed [31:0] size,
        input logic signed [31:0] bid,
        input logic signed [31:0] ask
    );
        begin
            buf_symbol <= symbol;
            buf_time <= tm;
            buf_price <= price;
            buf_size <= size;
            buf_bid <= bid;
            buf_ask <= ask;
        end
    endtask

    task automatic load_emit_from_buffer;
        begin
            emit_symbol <= buf_symbol;
            emit_time <= buf_time;
            emit_price <= buf_price;
            emit_size <= buf_size;
            emit_bid <= buf_bid;
            emit_ask <= buf_ask;
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

    task automatic reset_stream_state;
        begin
            state <= STATE_W0;
            group_valid <= 1'b0;
            quote_valid <= 1'b0;
            buf_valid <= 1'b0;
            emit_final <= 1'b0;
            post_emit_flush <= 1'b0;
            match_count <= 64'd0;
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
            rec_is_trade <= 1'b0;
            rec_symbol <= 32'd0;
            rec_time <= 64'd0;
            cur_symbol <= 32'd0;
            quote_time <= 64'd0;
            quote_bid <= 32'd0;
            quote_ask <= 32'd0;
            emit_symbol <= 32'd0;
            emit_time <= 64'd0;
            emit_price <= 32'd0;
            emit_size <= 32'd0;
            emit_bid <= 32'd0;
            emit_ask <= 32'd0;
            buf_symbol <= 32'd0;
            buf_time <= 64'd0;
            buf_price <= 32'd0;
            buf_size <= 32'd0;
            buf_bid <= 32'd0;
            buf_ask <= 32'd0;
        end
    endtask
endmodule

`default_nettype wire

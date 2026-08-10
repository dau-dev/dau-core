`default_nettype none

// Keyed rolling extremum (max or min) over a fixed count window, as a
// SEGMENTED Hillis-Steele network. Matches dau_core.temporal:rolling_max and
// :rolling_min bit-for-bit on a symbol-grouped stream.
//
// NOT named for a value type, deliberately. Extremum is the one aggregate
// whose result does not depend on the width it is computed in: comparing
// sign-extended int8/int16/int32 in a 32-bit container gives the same answer
// as comparing them as their own type, because sign extension is order
// preserving. So a single build serves every signed integer type that fits
// the value field, and VALUE_WIDTH selects the FRAMING (how many stream words
// a row occupies), not the arithmetic. That is the whole reason this tile
// carries no `int32-` prefix while its neighbours do -- theirs accumulate,
// and an accumulator's width is load-bearing.
//
// Framing, VALUE_WIDTH = 32 (one word per row):
//   w0 = {value[31:0], symbol_id[31:0]}
// VALUE_WIDTH = 64 (two words per row; a row is atomic, input_last only legal
// on the last word):
//   w0 = {value[31:0], symbol_id[31:0]}
//   w1 = {32'b0, value[63:32]}
//
// The stream must be symbol-GROUPED (rows for one symbol contiguous), which
// the host guarantees the same way it does for the moments tile. A symbol
// change is a segment boundary and resets the window for free: the network
// tracks a saturating run length since the boundary and stage k combines with
// the element 2**k back only when that element is inside the same segment.
//
// After K = $clog2(WINDOW) stages, position i holds the extremum over
// [max(i - WINDOW + 1, segment_start), i]. A row is emitted only once its
// window is FULL (run length >= WINDOW), so warm-up rows are dropped -- the
// golden returns None for exactly those rows. One row in, at most one row
// out, II=1, critical path one comparator plus one mux at every stage
// regardless of WINDOW.
//
// SILENT ON SUCCESS: a pure map, chainable mid-lane. Status fires only to
// report an error, so a chain closes out once at its terminal.
//
// WINDOW is a power of two so the network is exact: K Hillis-Steele stages
// span exactly 2**K elements, and a non-power-of-two window would need a
// prefix/suffix decomposition instead of a truncation that silently reports
// the wrong window.
module dau_rolling_extremum #(
    parameter int WINDOW = 4,
    parameter int VALUE_WIDTH = 32,
    parameter bit FIND_MAX = 1'b1
) (
    input  wire logic        clk,
    input  wire logic        rst,
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

    // The network is a FIXED-LATENCY pipeline, so backpressure freezes it
    // whole rather than just closing the input: a row already in flight would
    // otherwise keep advancing, reach the end while the skid slot is still
    // full, and be dropped with nothing reporting it.
    logic advance;
    localparam int K = $clog2(WINDOW);
    localparam int WORDS = (VALUE_WIDTH == 64) ? 2 : 1;
    // run length saturates at WINDOW: nothing above it changes a decision
    localparam int CW = $clog2(WINDOW + 1) + 1;

    if (WINDOW < 2 || WINDOW > 256) begin : g_window_range
        $error("WINDOW must be in [2, 256]");
    end
    if ((WINDOW & (WINDOW - 1)) != 0) begin : g_window_pow2
        $error("WINDOW must be a power of two");
    end
    if (VALUE_WIDTH != 32 && VALUE_WIDTH != 64) begin : g_value_width
        $error("VALUE_WIDTH must be 32 or 64");
    end

    typedef logic signed [VALUE_WIDTH-1:0] value_t;

    function automatic value_t better(input value_t a, input value_t b);
        // the ONLY place the max/min choice is made
        better = FIND_MAX ? ((a > b) ? a : b) : ((a < b) ? a : b);
    endfunction

    // ---- input assembly -------------------------------------------------
    logic [$clog2(WORDS+1)-1:0] word_idx;
    logic [31:0]                row_symbol;
    logic [31:0]                value_lo;
    logic                       row_fire;      // a complete row arrived
    value_t                     row_value;
    logic                       row_last;
    logic                       stream_error;

    assign input_ready = advance && !stream_error;
    wire input_fire = input_valid && input_ready;

    // a row torn by input_last before its last word is a malformed stream
    wire torn = input_fire && input_last && (WORDS == 2) && (word_idx == 0);

    always_ff @(posedge clk) begin
        if (rst) begin
            word_idx     <= '0;
            value_lo     <= '0;
            row_symbol   <= '0;
            stream_error <= 1'b0;
        end else if (input_fire && !stream_error) begin
            if (torn) begin
                stream_error <= 1'b1;
            end else if (WORDS == 1) begin
                word_idx <= '0;
            end else if (word_idx == 0) begin
                row_symbol <= input_data[31:0];
                value_lo   <= input_data[63:32];
                word_idx   <= word_idx + 1'b1;
            end else begin
                word_idx <= '0;
            end
        end
    end

    always_comb begin
        if (WORDS == 1) begin
            row_fire  = input_fire && !stream_error && !torn;
            row_value = value_t'(signed'(input_data[63:32]));
            row_last  = input_last;
        end else begin
            row_fire  = input_fire && !stream_error && !torn && (word_idx == 1);
            row_value = value_t'({input_data[31:0], value_lo});
            row_last  = input_last;
        end
    end

    wire [31:0] row_symbol_now = (WORDS == 1) ? input_data[31:0] : row_symbol;

    // ---- segment tracking ------------------------------------------------
    logic [31:0]        prev_symbol;
    logic               have_prev;
    logic [CW-1:0]      run_len;
    wire                new_segment = !have_prev || (row_symbol_now != prev_symbol);
    wire [CW-1:0]       run_next = new_segment ? CW'(1) : ((run_len >= CW'(WINDOW)) ? run_len : run_len + CW'(1));

    always_ff @(posedge clk) begin
        if (rst) begin
            prev_symbol <= '0;
            have_prev   <= 1'b0;
            run_len     <= '0;
        end else if (advance && row_fire) begin
            prev_symbol <= row_symbol_now;
            have_prev   <= 1'b1;
            run_len     <= run_next;
        end
    end

    // ---- the segmented Hillis-Steele network -----------------------------
    // stage[k] holds the value entering stage k; delay[k] is its 2**k-deep
    // line. Stage k combines with the element 2**k back only when the run
    // length says that element is in the same segment.
    value_t        stage_value [K+1];
    logic [CW-1:0] stage_run   [K+1];
    logic [31:0]   stage_symbol[K+1];
    logic          stage_valid [K+1];
    logic          stage_last  [K+1];

    assign stage_value[0]  = row_value;
    assign stage_run[0]    = run_next;
    assign stage_symbol[0] = row_symbol_now;
    assign stage_valid[0]  = row_fire;
    assign stage_last[0]   = row_last;

    generate
        for (genvar k = 0; k < K; k++) begin : g_stage
            localparam int DEPTH = 1 << k;
            value_t line [DEPTH];
            logic   line_valid [DEPTH];

            value_t        combined;
            logic [CW-1:0] run_q;
            logic [31:0]   symbol_q;
            logic          valid_q;
            logic          last_q;

            // the element DEPTH back is in this segment iff the run length
            // here already exceeds DEPTH
            wire in_segment = (stage_run[k] > CW'(DEPTH)) && line_valid[DEPTH-1];

            always_comb begin
                combined = in_segment ? better(stage_value[k], line[DEPTH-1]) : stage_value[k];
            end

            always_ff @(posedge clk) begin
                if (rst) begin
                    valid_q <= 1'b0;
                    for (int d = 0; d < DEPTH; d++) line_valid[d] <= 1'b0;
                end else if (advance) begin
                    valid_q <= stage_valid[k];
                    if (stage_valid[k]) begin
                        for (int d = DEPTH - 1; d > 0; d--) begin
                            line[d]       <= line[d-1];
                            line_valid[d] <= line_valid[d-1];
                        end
                        line[0]       <= stage_value[k];
                        line_valid[0] <= 1'b1;
                    end
                end
            end

            always_ff @(posedge clk) begin
                if (advance && stage_valid[k]) begin
                    run_q    <= stage_run[k];
                    symbol_q <= stage_symbol[k];
                    last_q   <= stage_last[k];
                end
            end

            value_t combined_q;
            always_ff @(posedge clk) begin
                if (advance && stage_valid[k]) combined_q <= combined;
            end

            assign stage_value[k+1]  = combined_q;
            assign stage_run[k+1]    = run_q;
            assign stage_symbol[k+1] = symbol_q;
            assign stage_valid[k+1]  = valid_q;
            assign stage_last[k+1]   = last_q;
        end
    endgenerate

    // ---- emission ---------------------------------------------------------
    // A row is emitted only once its window is FULL, so warm-up rows drop --
    // which means the LAST input row may not be an emitted one. The newest
    // emitted row is therefore held in a skid slot and released when a newer
    // one completes or the stream ends, so input_last always lands on the
    // final forwarded word even when trailing rows drop. Same contract the
    // row-predicate filter follows, and for the same reason: a consumer
    // cannot see the rows this tile chose not to forward.
    wire        emit_now    = stage_valid[K] && (stage_run[K] >= CW'(WINDOW));
    wire        stream_end  = stage_valid[K] && stage_last[K];
    // a CONTINUOUS assignment: `value_t emit_value = stage_value[K];` would
    // declare a variable with a static initializer evaluated once at time 0,
    // which reads as a permanent zero rather than the network's output
    value_t     emit_value;
    assign      emit_value  = stage_value[K];
    wire [31:0] emit_symbol = stage_symbol[K];
    // sliced width-safely: an indexed part-select is legal at both widths,
    // where a [VALUE_WIDTH-1:32] range is elaborated (and out of range) even
    // on the 32-bit build that never reaches it
    wire [31:0] emit_value_hi = emit_value[VALUE_WIDTH-1 -: 32];

    // latched once the final row has traversed the network, so a second
    // output word can still tell whether the stream is over
    logic stream_ended_q;
    always_ff @(posedge clk) begin
        if (rst) stream_ended_q <= 1'b0;
        else if (stream_end) stream_ended_q <= 1'b1;
    end

    logic        skid_valid;
    logic [63:0] skid_w0;
    logic [31:0] skid_hi;

    logic        out_valid_q;
    logic [63:0] out_data_q;
    logic        out_last_q;
    logic        out_second;   // a second word of this row is still owed
    logic [31:0] out_hi_q;
    logic        forwarded_any;

    assign output_valid = out_valid_q;
    assign output_data  = out_data_q;
    assign output_last  = out_last_q;

    wire output_fire = output_valid && output_ready;
    wire out_free    = !out_valid_q || output_fire;
    // the skid may release when the output side is free and no second word
    // of a previous row is still owed
    wire can_release = out_free && !out_second;
    assign advance = !skid_valid || can_release;

    always_ff @(posedge clk) begin
        if (rst) begin
            skid_valid    <= 1'b0;
            out_valid_q   <= 1'b0;
            out_second    <= 1'b0;
            out_last_q    <= 1'b0;
            forwarded_any <= 1'b0;
        end else begin
            if (output_fire) out_valid_q <= 1'b0;

            if (out_second && out_free) begin
                // second word of the row already begun; it carries last when
                // the stream has ended and nothing is queued behind it
                out_data_q  <= {32'b0, out_hi_q};
                out_last_q  <= stream_ended_q && !skid_valid && !emit_now;
                out_valid_q <= 1'b1;
                out_second  <= 1'b0;
            end else if (can_release && skid_valid && (emit_now || stream_end || stream_ended_q)) begin
                // release the held row. It is last only once the stream has
                // ended AND nothing newer is arriving to take its place --
                // including the case where the FINAL row is itself an
                // emitting one, which loads the skid on the same cycle the
                // stream ends and is flushed on the next.
                out_data_q    <= skid_w0;
                out_hi_q      <= skid_hi;
                out_last_q    <= (stream_end || stream_ended_q) && !emit_now && (WORDS == 1);
                out_valid_q   <= 1'b1;
                out_second    <= (WORDS == 2);
                forwarded_any <= 1'b1;
                skid_valid    <= emit_now;
                if (emit_now) begin
                    skid_w0  <= {emit_value[31:0], emit_symbol};
                    skid_hi  <= emit_value_hi;
                end
            end else if (emit_now && !skid_valid) begin
                skid_w0    <= {emit_value[31:0], emit_symbol};
                skid_hi    <= emit_value_hi;
                skid_valid <= 1'b1;
            end
        end
    end

    // ---- status ------------------------------------------------------------
    // SILENT ON SUCCESS while it forwards rows. A stream that ends having
    // forwarded NOTHING still closes out, because zero forwarded rows is the
    // one outcome a downstream tile cannot distinguish from a stall.
    logic status_valid_q;
    logic status_error_q;
    assign status_valid      = status_valid_q;
    assign status_error      = status_error_q;
    assign status_error_code = status_error_q ? ERR_STREAM : 8'd0;

    always_ff @(posedge clk) begin
        if (rst) begin
            status_valid_q <= 1'b0;
            status_error_q <= 1'b0;
        end else if (torn) begin
            status_valid_q <= 1'b1;
            status_error_q <= 1'b1;
        end else if ((stream_end || stream_ended_q) && !forwarded_any && !skid_valid && !emit_now) begin
            status_valid_q <= 1'b1;
            status_error_q <= 1'b0;
        end else if (status_valid_q && status_ready) begin
            status_valid_q <= 1'b0;
        end
    end

endmodule

`default_nettype wire

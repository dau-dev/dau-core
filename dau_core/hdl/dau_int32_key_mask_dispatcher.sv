`default_nettype none

// Key-mask dispatcher for 16-byte int32 quad rows: each whole row routes to
// exactly one of NUM_PARTITIONS output streams by its key's low bits —
// dest = key & (NUM_PARTITIONS - 1), NUM_PARTITIONS a power of two. This is
// the broadcast + per-lane pair-key-filter shape (key & mask == lane, the
// silicon-proven q6 partition) folded into ONE shared router: each row is
// delivered once instead of N times, so the shared segment stops paying the
// every-lane-consumes-every-row cost that capped the broadcast fan-out at the
// per-lane two-beat rate. Rows keep input order within a partition; every
// lane carries the DAU stream+status contract independently, so a dispatched
// scan drops straight into the existing NoC lane model.
//
// IN_WIDTH (the width-tier catalog knob): 64 (default) accepts the classic
// two-beat quad rows (w0 = {value, key}, then w1 = {aux1, aux0}) — at most
// one row per 2 cycles. 128/256/512 accept 1/2/4 whole quad rows per beat
// (row i at input_data[i*128 +: 128]; [63:0] = {value, key},
// [127:64] = {aux1, aux0}). 256/512 DESERIALIZE the R-row beat onto an
// internal single-row feed (one row per cycle, lowest index first) that
// drives the SAME proven routing datapath as 128 — so a wide reader's beats
// deliver DDR bandwidth (fewer, wider bursts on the AXI side) while the
// dispatch front stays at the proven 1 row/cycle. TRUE multi-row/cycle
// dispatch (parallel routing of a beat's rows to distinct lanes, > 1
// row/cycle) is a named follow-up; today the serializer matches 128's rate.
// The all-ones 128-bit row is the reserved HOST PAD ROW (wide beats force
// beat-aligned lengths; ragged tails pad with 0xFF) and is dropped by the
// serializer, never routed. Per-lane outputs stay the 64-bit two-beat
// framing the operator tiles consume: each lane sees only ~1/N of the rows,
// so NO operator widens. A run of same-lane rows throttles on that lane's
// two-beat drain (single-row skid, head-of-line like the range partitioner);
// mask-distributed keys sustain the full front rate. Any other IN_WIDTH, or a
// non-power-of-two NUM_PARTITIONS, fails elaboration.
//
// Row atomicity (IN_WIDTH=64): input_last is only legal on w1 of a row; last
// on a w0 is a torn row — the whole batch is invalid and every lane reports
// ERR_STREAM. At IN_WIDTH=128 a beat IS a complete row, so torn framing
// cannot occur. A lane that receives zero rows closes out with a success
// status (its downstream tile never starts); a lane that received rows lands
// input_last on its own final forwarded word.
// dau_core.operators.mask_dispatch_rows is the golden.

// One partition's back half: a single-row skid that holds the newest row so
// its stream-end can be tagged last, plus the zero-row / torn-row status the
// downstream tile cannot see. A self-contained copy of
// dau_range_partition_lane (renamed so a composition carrying both cores does
// not collide), keeping the dispatcher a one-file core.
module dau_key_mask_dispatch_lane #(
    parameter int unsigned RECORD_WORDS = 2  // 64-bit words per record (2 = the quad row)
) (
    input  wire logic        clk,
    input  wire logic        rst,
    // a completed record destined for this lane (asserted by the shared front)
    input  wire logic        push_valid,
    output logic             push_ready,
    input  wire logic [RECORD_WORDS*64-1:0] push_words,
    // batch control (levels, driven while the front drains): flush ends the
    // batch (emit the held row with last=1, or close out zero-row), err marks
    // a torn batch (raise ERR_STREAM, drop everything)
    input  wire logic        flush,
    input  wire logic        err,
    output logic             busy,
    // partition output row stream
    output logic             output_valid,
    input  wire logic        output_ready,
    output logic [63:0]      output_data,
    output logic             output_last,
    // partition status
    output logic             status_valid,
    input  wire logic        status_ready,
    output logic             status_error,
    output logic [7:0]       status_error_code
);
    localparam logic [7:0] ERR_STREAM = 8'd2;

    localparam int unsigned WIDX_W = (RECORD_WORDS <= 1) ? 1 : $clog2(RECORD_WORDS);

    typedef enum logic [1:0] {
        EMIT_IDLE,
        EMIT_WORDS
    } emit_t;

    emit_t              emit_state;
    logic [WIDX_W-1:0]  emit_index;  // which word of the record is next out
    logic [RECORD_WORDS*64-1:0] emit_words;
    logic                       emit_last;

    logic                       held_valid;
    logic [RECORD_WORDS*64-1:0] held_words;
    logic                       drain_started;  // flush/err action taken once per batch end

    logic output_fire;
    logic emit_free;

    assign output_fire = output_valid && output_ready;
    // the emit engine can take a displaced row when idle, or on the cycle its
    // final word is accepted
    assign emit_free = (emit_state == EMIT_IDLE) || ((emit_state == EMIT_WORDS) && output_fire && (emit_index == WIDX_W'(RECORD_WORDS - 1)));

    // never accept a new push while ending the batch
    assign push_ready = !flush && !err && (!held_valid || emit_free);

    assign output_valid = emit_state != EMIT_IDLE;
    assign output_data = emit_words[64 * emit_index +: 64];
    assign output_last = (emit_index == WIDX_W'(RECORD_WORDS - 1)) && emit_last;

    // a lane is done only once its held row, emit engine, and status have all
    // drained — the front waits on this before starting the next batch. The
    // trailing term keeps the lane busy from the first flush/err cycle (before
    // its status_valid registers) so the front cannot leave the batch-end
    // states early and abandon a pending close-out.
    assign busy = held_valid || (emit_state != EMIT_IDLE) || status_valid || ((flush || err) && !drain_started);

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_lane();
        end else begin
            // emit engine advance (runs regardless of batch state so the final
            // row drains during flush)
            case (emit_state)
                EMIT_WORDS:
                    if (output_fire) begin
                        if (emit_index == WIDX_W'(RECORD_WORDS - 1)) emit_state <= EMIT_IDLE;
                        else emit_index <= emit_index + WIDX_W'(1);
                    end
                default: ;
            endcase

            // status handshake
            if (status_valid && status_ready) begin
                status_valid <= 1'b0;
                status_error <= 1'b0;
                status_error_code <= 8'd0;
            end

            if (err) begin
                // torn batch: raise ERR_STREAM once, drop held + in-flight
                if (!drain_started) begin
                    held_valid <= 1'b0;
                    emit_state <= EMIT_IDLE;
                    status_valid <= 1'b1;
                    status_error <= 1'b1;
                    status_error_code <= ERR_STREAM;
                    drain_started <= 1'b1;
                end
            end else if (flush) begin
                // batch end: emit the held row as last, or close out a lane
                // that never received a row. held_valid==0 here iff zero rows
                // arrived (the newest row is always held until flush).
                if (!drain_started && emit_free) begin
                    if (held_valid) begin
                        start_emit(held_words, 1'b1);
                        held_valid <= 1'b0;
                    end else begin
                        status_valid <= 1'b1;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                    end
                    drain_started <= 1'b1;
                end
            end else begin
                // running: batch boundary passed, re-arm for the next batch
                drain_started <= 1'b0;
                if (push_valid && push_ready) begin
                    if (held_valid) start_emit(held_words, 1'b0);
                    held_valid <= 1'b1;
                    held_words <= push_words;
                end
            end
        end
    end

    task automatic start_emit(input logic [RECORD_WORDS*64-1:0] words, input logic last_row);
        begin
            emit_words <= words;
            emit_last <= last_row;
            emit_index <= '0;
            emit_state <= EMIT_WORDS;
        end
    endtask

    task automatic reset_lane;
        begin
            emit_state <= EMIT_IDLE;
            emit_index <= '0;
            emit_words <= '0;
            emit_last <= 1'b0;
            held_valid <= 1'b0;
            held_words <= '0;
            drain_started <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask
endmodule


// EXACT_BRANCH (the q19 disjunct-branch mode): dest = key - 1 for keys in
// [1, NUM_PARTITIONS]; every other key (branch 0, or any key whose unsigned
// 32-bit image is out of range) is DROPPED at the front for zero lane
// cycles — the dominant no-branch class never throttles the wide front on a
// lane's two-beat drain. NUM_PARTITIONS needs no power-of-two in this mode.
module dau_int32_key_mask_dispatcher #(
    parameter int NUM_PARTITIONS = 4,
    parameter int unsigned IN_WIDTH = 64,
    parameter bit EXACT_BRANCH = 1'b0,
    // 64-bit words per RECORD. 2 is the 16-byte quad row (every existing
    // composition). A fused chain whose head reads wider records — the as-of
    // join's 3-word {tag/key, time, payload} — routes those records whole,
    // keyed on word 0's low half, without any reshaping. Only the 64-bit
    // framing path supports RECORD_WORDS != 2: the wide beat framings are
    // defined in whole 128-bit quad rows.
    parameter int unsigned RECORD_WORDS = 2,
    // WHOLE-RECORD INPUT (the gearbox pairing). 0 = the historical framings:
    // 64-bit words one per cycle, so an N-word record costs N cycles; or
    // 128-bit quad rows. 1 = input_data carries ONE WHOLE RECORD per beat on
    // a RECORD_WORDS*64-bit bus, so the router ingests a record per cycle.
    // That is the point of the pairing: replicated lanes can absorb a record
    // per cycle collectively, and a word-at-a-time front starves them.
    parameter bit RECORD_INPUT = 1'b0
) (
    input  wire logic        clk,
    input  wire logic        rst,
    // input row stream: two-beat quad rows at IN_WIDTH=64, one whole row per
    // beat at IN_WIDTH=128 (key = bits [31:0] either way)
    input  wire logic        input_valid,
    output logic             input_ready,
    input  wire logic [IN_WIDTH-1:0] input_data,
    input  wire logic        input_last,
    // NUM_PARTITIONS output row streams (bit/word p is partition p)
    output logic [NUM_PARTITIONS-1:0]        output_valid,
    input  wire logic [NUM_PARTITIONS-1:0]   output_ready,
    output logic [NUM_PARTITIONS*64-1:0]     output_data,
    output logic [NUM_PARTITIONS-1:0]        output_last,
    // NUM_PARTITIONS status streams
    output logic [NUM_PARTITIONS-1:0]        status_valid,
    input  wire logic [NUM_PARTITIONS-1:0]   status_ready,
    output logic [NUM_PARTITIONS-1:0]        status_error,
    output logic [NUM_PARTITIONS*8-1:0]      status_error_code
);
    // in whole-record mode the input bus IS the record, not a beat of quad
    // rows, so the row-oriented wide/serializer paths do not apply
    localparam bit WIDE = (IN_WIDTH >= 128) && !RECORD_INPUT;
    localparam bit MULTI = (IN_WIDTH > 128) && !RECORD_INPUT;          // 256/512: R>1 rows per beat
    localparam int R = MULTI ? (IN_WIDTH / 128) : 1;  // whole quad rows per input beat
    localparam int PIDX_W = (NUM_PARTITIONS <= 1) ? 1 : $clog2(NUM_PARTITIONS);

    if (!(IN_WIDTH inside {64, 128, 256, 512})) begin : g_bad_in_width
        $fatal(1, "dau_int32_key_mask_dispatcher: IN_WIDTH must be 64/128/256/512");
    end
    if (NUM_PARTITIONS < 1) begin : g_bad_partition_count
        $fatal(1, "dau_int32_key_mask_dispatcher: NUM_PARTITIONS must be positive");
    end
    if (RECORD_INPUT && IN_WIDTH != RECORD_WORDS * 64) begin : g_bad_record_input
        $fatal(1, "dau_int32_key_mask_dispatcher: RECORD_INPUT needs IN_WIDTH == RECORD_WORDS*64 (one whole record per beat)");
    end
    if (RECORD_WORDS < 1 || RECORD_WORDS > 16) begin : g_bad_record_words
        $fatal(1, "dau_int32_key_mask_dispatcher: RECORD_WORDS must be in [1, 16]");
    end
    if (!RECORD_INPUT && IN_WIDTH != 64 && RECORD_WORDS != 2) begin : g_wide_needs_quad
        $fatal(1, "dau_int32_key_mask_dispatcher: wide framings carry whole 128-bit quad rows (RECORD_WORDS must be 2)");
    end
    if (!EXACT_BRANCH && (NUM_PARTITIONS & (NUM_PARTITIONS - 1)) != 0) begin : g_bad_partitions
        $fatal(1, "dau_int32_key_mask_dispatcher: NUM_PARTITIONS must be a power of two in mask mode");
    end

    typedef enum logic [1:0] {
        F_W0,   // accept w0 of a row (WIDE: accept + route the whole row)
        F_W1,   // accept w1, route the completed row (narrow only)
        F_END,  // batch ended cleanly: flush every lane
        F_ERR   // torn batch: error every lane (narrow only — a wide beat is
                // a complete row, so torn framing cannot occur)
    } front_state_t;

    front_state_t fstate;

    logic input_fire;
    assign input_fire = input_valid && input_ready;

    // per-lane wires
    logic [NUM_PARTITIONS-1:0] lane_push_valid;
    logic [NUM_PARTITIONS-1:0] lane_push_ready;
    logic [NUM_PARTITIONS-1:0] lane_busy;
    logic [RECORD_WORDS*64-1:0] push_words;

    logic all_idle;
    assign all_idle = (lane_busy == '0);

    // mask decode: dest = key & (NUM_PARTITIONS - 1) — the key's low
    // $clog2(NUM_PARTITIONS) bits (a power of two makes & and bit-select the
    // same thing; a degenerate single partition routes everything to lane 0).
    // Exact-branch decode: dest = key - 1, out-of-range keys drop (route_hit
    // low accepts the row without pushing any lane).
    logic [PIDX_W-1:0] dest;
    logic [31:0]       route_key;
    logic              route_hit;

    function automatic logic [PIDX_W-1:0] decode_dest(input logic [31:0] key);
        if (EXACT_BRANCH) return PIDX_W'(key - 32'd1);
        return (NUM_PARTITIONS <= 1) ? '0 : key[PIDX_W-1:0];
    endfunction

    assign route_hit = !EXACT_BRANCH || (route_key != 32'd0 && route_key <= 32'(NUM_PARTITIONS));

    // The wide path routes ONE 128-bit quad row at a time. At IN_WIDTH=128 the
    // input beat IS that row; at 256/512 a serializer deserializes the R-row
    // beat onto this internal single-row feed (one row per cycle, pad rows
    // dropped), so the proven 1-row-per-cycle routing/lane datapath is reused
    // unchanged — width is a front-end concern, not a routing one.
    logic         wrow_valid;
    logic         wrow_ready;
    logic [127:0] wrow_data;
    logic         wrow_last;
    logic         wrow_fire;
    logic         flush_batch;   // end the batch when a last beat carries no real row
    assign wrow_fire = wrow_valid && wrow_ready;

    if (MULTI) begin : g_serializer
        // hold one accepted beat, present its non-pad sub-rows lowest-index
        // first; input_last rides the LAST non-pad row of the last beat. The
        // all-ones pad row (ragged-tail padding) is skipped, never forwarded.
        logic [127:0] sbeat [R];
        logic [R-1:0] svalid;   // sub-rows not yet forwarded (pad excluded)
        logic         sbeat_last;
        logic         loaded;

        // lowest set index of svalid, and the remaining set after it clears
        logic [31:0]  sidx;
        logic [R-1:0] svalid_next;
        always_comb begin
            sidx = '0;
            for (int i = R - 1; i >= 0; i--)
                if (svalid[i]) sidx = i;
        end
        assign svalid_next = svalid & ~(loaded ? (({{(R-1){1'b0}}, 1'b1}) << sidx) : '0);

        // an accepted beat with no non-pad rows (all-pad — a defensive case;
        // valid host framing only pads the LAST beat's tail and never a whole
        // beat) is consumed in one cycle instead of wedging the feed
        logic empty_loaded;
        assign empty_loaded = loaded && (svalid == '0);

        assign wrow_valid = loaded && (svalid != '0);
        assign wrow_data = sbeat[sidx];
        // last only on the final non-pad row of a last-flagged beat
        assign wrow_last = sbeat_last && (svalid_next == '0);
        // batch end when an all-pad beat was the last beat: no row carries
        // wrow_last, so the front is flushed directly
        assign flush_batch = empty_loaded && sbeat_last;
        // accept a new beat when idle, the cycle the last row leaves, or the
        // cycle an all-pad beat is consumed
        assign input_ready = !loaded || (wrow_fire && (svalid_next == '0)) || empty_loaded;

        always_ff @(posedge clk) begin
            if (rst) begin
                loaded <= 1'b0;
                svalid <= '0;
                sbeat_last <= 1'b0;
            end else begin
                if (loaded && wrow_fire) begin
                    svalid <= svalid_next;
                    if (svalid_next == '0) loaded <= 1'b0;
                end else if (empty_loaded) begin
                    loaded <= 1'b0;  // drop the all-pad beat
                end
                if (input_valid && input_ready) begin
                    for (int i = 0; i < R; i++) begin
                        sbeat[i] <= input_data[i*128 +: 128];
                        svalid[i] <= (input_data[i*128 +: 128] != {128{1'b1}});
                    end
                    sbeat_last <= input_last;
                    loaded <= 1'b1;
                end
            end
        end
    end else if (WIDE) begin : g_serializer_passthrough
        // IN_WIDTH=128: the input beat is exactly one row; forward directly
        assign wrow_valid = input_valid;
        assign input_ready = wrow_ready;
        assign wrow_data = input_data[127:0];
        assign wrow_last = input_last;
        assign flush_batch = 1'b0;  // 128 carries end on the (single) row
    end else begin : g_no_serializer
        // IN_WIDTH=64: the NARROW front owns the input handshake and builds
        // records word by word, so no row feed exists here. This branch must
        // exist and must NOT drive input_ready: elaborating the 128-bit
        // passthrough at 64 both slices input_data out of range (a hard
        // Vivado synthesis error, only a warning under verilator) and adds a
        // second driver on input_ready.
        assign wrow_valid = 1'b0;
        assign wrow_data = 128'd0;
        assign wrow_last = 1'b0;
        assign flush_batch = 1'b0;
    end

    if (WIDE) begin : g_front_wide
        // one whole row per cycle off the single-row feed: route it the cycle
        // it is accepted — the front sustains 1 row/cycle whenever the
        // destination lane can take it, and a dropped row costs one cycle.
        // This is the proven R=1 datapath; MULTI just feeds it from the
        // serializer instead of directly from the input beat.
        assign route_key = wrow_data[31:0];
        assign dest = decode_dest(route_key);
        assign push_words = wrow_data[RECORD_WORDS*64-1:0];
        assign wrow_ready = (fstate == F_W0) && (!route_hit || lane_push_ready[dest]);
        always_comb begin
            lane_push_valid = '0;
            if (fstate == F_W0 && wrow_fire && route_hit) lane_push_valid[dest] = 1'b1;
        end
        always_ff @(posedge clk) begin
            if (rst) begin
                fstate <= F_W0;
            end else begin
                case (fstate)
                    F_W0: if ((wrow_fire && wrow_last) || flush_batch) fstate <= F_END;
                    F_END: if (all_idle) fstate <= F_W0;
                    default: fstate <= F_W0;
                endcase
            end
        end
    end else if (RECORD_INPUT) begin : g_front_record
        // WHOLE-RECORD FRONT: the input beat IS one record, so route it the
        // cycle it is accepted — one record per cycle whenever the
        // destination lane can take it. Structurally the proven wide front,
        // with the record bus in place of the 128-bit quad row.
        assign route_key = input_data[31:0];
        assign dest = decode_dest(route_key);
        assign push_words = input_data[RECORD_WORDS*64-1:0];
        always_comb begin
            input_ready = 1'b0;
            case (fstate)
                F_W0: input_ready = !route_hit || lane_push_ready[dest];
                default: input_ready = 1'b0;
            endcase
        end
        always_comb begin
            lane_push_valid = '0;
            if (fstate == F_W0 && input_fire && route_hit) lane_push_valid[dest] = 1'b1;
        end
        always_ff @(posedge clk) begin
            if (rst) begin
                fstate <= F_W0;
            end else begin
                case (fstate)
                    F_W0:   if (input_fire && input_last) fstate <= F_END;
                    F_END:  if (all_idle) fstate <= F_W0;
                    default: fstate <= F_W0;
                endcase
            end
        end
    end else begin : g_front_narrow
        // multi-beat records: collect RECORD_WORDS words, keyed on word 0's
        // low half, and route as the FINAL word arrives (the range
        // partitioner's front, with the mask decode in place of the splitter
        // compare tree). RECORD_WORDS=2 keeps the original two-beat cadence.
        localparam int unsigned NIDX_W = (RECORD_WORDS <= 1) ? 1 : $clog2(RECORD_WORDS);
        logic [RECORD_WORDS*64-1:0] cur_words;
        logic [NIDX_W-1:0]          cur_index;   // which word of the record is next in
        logic                       last_word;

        assign last_word = (cur_index == NIDX_W'(RECORD_WORDS - 1));
        assign route_key = cur_words[31:0];
        assign dest = decode_dest(route_key);
        // the final word is not yet registered when it is routed, so present
        // it directly beside the words already collected
        always_comb begin
            push_words = cur_words;
            push_words[64 * cur_index +: 64] = input_data[63:0];
        end
        always_comb begin
            input_ready = 1'b0;
            case (fstate)
                F_W0: input_ready = !last_word || !route_hit || lane_push_ready[dest];
                default: input_ready = 1'b0;
            endcase
        end
        always_comb begin
            lane_push_valid = '0;
            if (fstate == F_W0 && input_fire && last_word && route_hit) lane_push_valid[dest] = 1'b1;
        end
        always_ff @(posedge clk) begin
            if (rst) begin
                fstate <= F_W0;
                cur_words <= '0;
                cur_index <= '0;
            end else begin
                case (fstate)
                    F_W0: begin
                        if (input_fire) begin
                            cur_words[64 * cur_index +: 64] <= input_data[63:0];
                            if (last_word) begin
                                cur_index <= '0;
                                fstate <= input_last ? F_END : F_W0;
                            end else if (input_last) begin
                                fstate <= F_ERR;  // torn record (last before its final word)
                            end else begin
                                cur_index <= cur_index + NIDX_W'(1);
                            end
                        end
                    end
                    F_END:  if (all_idle) begin cur_index <= '0; fstate <= F_W0; end
                    F_ERR:  if (all_idle) begin cur_index <= '0; fstate <= F_W0; end
                    default: fstate <= F_W0;
                endcase
            end
        end
    end

    genvar p;
    generate
        for (p = 0; p < NUM_PARTITIONS; p++) begin : g_lane
            dau_key_mask_dispatch_lane #(
                .RECORD_WORDS(RECORD_WORDS)
            ) u_lane (
                .clk(clk),
                .rst(rst),
                .push_valid(lane_push_valid[p]),
                .push_ready(lane_push_ready[p]),
                .push_words(push_words),
                .flush(fstate == F_END),
                .err(fstate == F_ERR),
                .busy(lane_busy[p]),
                .output_valid(output_valid[p]),
                .output_ready(output_ready[p]),
                .output_data(output_data[p*64 +: 64]),
                .output_last(output_last[p]),
                .status_valid(status_valid[p]),
                .status_ready(status_ready[p]),
                .status_error(status_error[p]),
                .status_error_code(status_error_code[p*8 +: 8])
            );
        end
    endgenerate
endmodule

`default_nettype wire

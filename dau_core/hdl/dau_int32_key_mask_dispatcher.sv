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
// IN_WIDTH (the rows/cycle axis): 64 (default) accepts the classic two-beat
// quad rows (w0 = {value, key}, then w1 = {aux1, aux0}) — at most one row per
// 2 cycles, matching the two-beat stream. 128 accepts ONE whole row per beat
// ({w1, w0}: [63:0] = {value, key}, [127:64] = {aux1, aux0}) — paired with
// the wide row-unpack (OUT_WIDTH=128) the shared front sustains 1 row/cycle,
// while the per-lane outputs stay the 64-bit two-beat framing the operator
// tiles consume: each lane sees only ~1/N of the rows, so NO operator widens.
// A run of same-lane rows throttles on that lane's two-beat drain (single-row
// skid, head-of-line like the range partitioner); mask-distributed keys
// sustain the full front rate. Any other IN_WIDTH, or a non-power-of-two
// NUM_PARTITIONS, fails elaboration.
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
module dau_key_mask_dispatch_lane (
    input  wire logic        clk,
    input  wire logic        rst,
    // a completed row destined for this lane (asserted by the shared front)
    input  wire logic        push_valid,
    output logic             push_ready,
    input  wire logic [63:0] push_w0,
    input  wire logic [63:0] push_w1,
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

    typedef enum logic [1:0] {
        EMIT_IDLE,
        EMIT_W0,
        EMIT_W1
    } emit_t;

    emit_t       emit_state;
    logic [63:0] emit_w0;
    logic [63:0] emit_w1;
    logic        emit_last;

    logic        held_valid;
    logic [63:0] held_w0;
    logic [63:0] held_w1;
    logic        drain_started;  // flush/err action taken once per batch end

    logic output_fire;
    logic emit_free;

    assign output_fire = output_valid && output_ready;
    // the emit engine can take a displaced row when idle, or on the cycle its
    // final word is accepted
    assign emit_free = (emit_state == EMIT_IDLE) || ((emit_state == EMIT_W1) && output_fire);

    // never accept a new push while ending the batch
    assign push_ready = !flush && !err && (!held_valid || emit_free);

    assign output_valid = emit_state != EMIT_IDLE;
    assign output_data = (emit_state == EMIT_W0) ? emit_w0 : emit_w1;
    assign output_last = (emit_state == EMIT_W1) && emit_last;

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
                EMIT_W0: if (output_fire) emit_state <= EMIT_W1;
                EMIT_W1: if (output_fire) emit_state <= EMIT_IDLE;
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
                        start_emit(held_w0, held_w1, 1'b1);
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
                    if (held_valid) start_emit(held_w0, held_w1, 1'b0);
                    held_valid <= 1'b1;
                    held_w0 <= push_w0;
                    held_w1 <= push_w1;
                end
            end
        end
    end

    task automatic start_emit(input logic [63:0] w0, input logic [63:0] w1, input logic last_row);
        begin
            emit_w0 <= w0;
            emit_w1 <= w1;
            emit_last <= last_row;
            emit_state <= EMIT_W0;
        end
    endtask

    task automatic reset_lane;
        begin
            emit_state <= EMIT_IDLE;
            emit_w0 <= 64'd0;
            emit_w1 <= 64'd0;
            emit_last <= 1'b0;
            held_valid <= 1'b0;
            held_w0 <= 64'd0;
            held_w1 <= 64'd0;
            drain_started <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask
endmodule


module dau_int32_key_mask_dispatcher #(
    parameter int NUM_PARTITIONS = 4,
    parameter int unsigned IN_WIDTH = 64
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
    localparam bit WIDE = (IN_WIDTH == 128);
    localparam int PIDX_W = (NUM_PARTITIONS <= 1) ? 1 : $clog2(NUM_PARTITIONS);

    if (IN_WIDTH != 64 && IN_WIDTH != 128) begin : g_bad_in_width
        $fatal(1, "dau_int32_key_mask_dispatcher: IN_WIDTH must be 64 or 128");
    end
    if (NUM_PARTITIONS < 1 || (NUM_PARTITIONS & (NUM_PARTITIONS - 1)) != 0) begin : g_bad_partitions
        $fatal(1, "dau_int32_key_mask_dispatcher: NUM_PARTITIONS must be a power of two");
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
    logic [63:0]               push_w0;
    logic [63:0]               push_w1;

    logic all_idle;
    assign all_idle = (lane_busy == '0);

    // mask decode: dest = key & (NUM_PARTITIONS - 1) — the key's low
    // $clog2(NUM_PARTITIONS) bits (a power of two makes & and bit-select the
    // same thing; a degenerate single partition routes everything to lane 0)
    logic [PIDX_W-1:0] dest;

    if (WIDE) begin : g_front_wide
        // one whole row per beat: route it the cycle it is accepted — the
        // front sustains 1 row/cycle whenever the destination lane can take it
        assign dest = (NUM_PARTITIONS <= 1) ? '0 : input_data[PIDX_W-1:0];
        assign push_w0 = input_data[63:0];
        assign push_w1 = input_data[127:64];
        assign input_ready = (fstate == F_W0) && lane_push_ready[dest];
        always_comb begin
            lane_push_valid = '0;
            if (fstate == F_W0 && input_fire) lane_push_valid[dest] = 1'b1;
        end
        always_ff @(posedge clk) begin
            if (rst) begin
                fstate <= F_W0;
            end else begin
                case (fstate)
                    F_W0: if (input_fire && input_last) fstate <= F_END;
                    F_END: if (all_idle) fstate <= F_W0;
                    default: fstate <= F_W0;
                endcase
            end
        end
    end else begin : g_front_narrow
        // two-beat rows: latch w0, route on w1 (the range partitioner's front,
        // with the mask decode in place of the splitter compare tree)
        logic [63:0] cur_w0;
        assign dest = (NUM_PARTITIONS <= 1) ? '0 : cur_w0[PIDX_W-1:0];
        assign push_w0 = cur_w0;
        assign push_w1 = input_data[63:0];
        always_comb begin
            input_ready = 1'b0;
            case (fstate)
                F_W0: input_ready = 1'b1;
                F_W1: input_ready = lane_push_ready[dest];
                default: input_ready = 1'b0;
            endcase
        end
        always_comb begin
            lane_push_valid = '0;
            if (fstate == F_W1 && input_fire) lane_push_valid[dest] = 1'b1;
        end
        always_ff @(posedge clk) begin
            if (rst) begin
                fstate <= F_W0;
                cur_w0 <= 64'd0;
            end else begin
                case (fstate)
                    F_W0: begin
                        if (input_fire) begin
                            if (input_last) begin
                                fstate <= F_ERR;  // torn row (last on w0)
                            end else begin
                                cur_w0 <= input_data[63:0];
                                fstate <= F_W1;
                            end
                        end
                    end
                    F_W1: begin
                        if (input_fire) begin
                            fstate <= input_last ? F_END : F_W0;
                        end
                    end
                    F_END:  if (all_idle) fstate <= F_W0;
                    F_ERR:  if (all_idle) fstate <= F_W0;
                    default: fstate <= F_W0;
                endcase
            end
        end
    end

    genvar p;
    generate
        for (p = 0; p < NUM_PARTITIONS; p++) begin : g_lane
            dau_key_mask_dispatch_lane u_lane (
                .clk(clk),
                .rst(rst),
                .push_valid(lane_push_valid[p]),
                .push_ready(lane_push_ready[p]),
                .push_w0(push_w0),
                .push_w1(push_w1),
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

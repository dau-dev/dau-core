`default_nettype none

// Streaming range partitioner for 16-byte pair rows (two 64-bit words,
// w0 = {value, key}). Each whole row is routed to exactly one of
// NUM_PARTITIONS output streams by its signed 32-bit key: with k =
// NUM_PARTITIONS-1 monotonic non-decreasing splitters, partition p receives
// keys in [splitter[p-1], splitter[p]) (p==0 is key < splitter[0], the last
// partition is key >= splitter[k-1]). This generalizes the pair-key filter
// (one keep/drop mask) into the HARP range-partition primitive: the same one
// input scan fans out to N key-ordered lanes.
//
// Guarantees: partitions are key-ordered (partition i's key range precedes
// i+1's — the enabler for global sort by concatenation) and each partition is
// stable (rows keep input order within it). Every lane carries the DAU
// stream+status contract independently, so a partitioned scan drops straight
// into the existing NoC lane model.
//
// Row atomicity: input_last is only legal on w1 of a row. input_last on a w0
// is a torn row — the whole batch is invalid and every lane reports
// ERR_STREAM. A lane that receives zero rows closes out with a success status
// (its downstream tile never starts), mirroring the pair-key filter; a lane
// that received rows lands input_last on its own final forwarded word.

// One partition's back half: a single-row skid that holds the newest row so
// its stream-end can be tagged last, plus the zero-row / torn-row status the
// downstream tile cannot see. Reused NUM_PARTITIONS times by the top.
module dau_range_partition_lane (
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
    // its status_valid registers) so the front cannot leave F_END/F_ERR early
    // and abandon a pending close-out (e.g. a torn first row on an idle lane).
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


module dau_int32_range_partitioner #(
    parameter int NUM_PARTITIONS = 4
) (
    input  wire logic        clk,
    input  wire logic        rst,
    // k = NUM_PARTITIONS-1 signed 32-bit splitters, monotonic non-decreasing.
    // cfg_splitters[i*32 +: 32] is splitter i. Width is clamped to >=1 bit so a
    // degenerate single-partition instance (no splitters) stays legal; the
    // decode loop then never reads it.
    input  wire logic [((NUM_PARTITIONS > 1) ? (NUM_PARTITIONS-1)*32 : 1)-1:0] cfg_splitters,
    // input row stream (w0 = {value, key}, key = w0[31:0])
    input  wire logic        input_valid,
    output logic             input_ready,
    input  wire logic [63:0] input_data,
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
    localparam int PIDX_W = (NUM_PARTITIONS <= 1) ? 1 : $clog2(NUM_PARTITIONS);

    typedef enum logic [1:0] {
        F_W0,   // accept w0 of a row
        F_W1,   // accept w1, route the completed row
        F_END,  // batch ended cleanly: flush every lane
        F_ERR   // torn batch: error every lane
    } front_state_t;

    front_state_t fstate;
    logic [63:0]  cur_w0;

    logic input_fire;

    // HARP linear decode: destination partition = number of splitters the key
    // is >= (signed). Monotonic splitters make this the key's range index.
    logic [PIDX_W-1:0] dest;
    always_comb begin
        dest = '0;
        for (int i = 0; i < NUM_PARTITIONS - 1; i++) begin
            if ($signed(cur_w0[31:0]) >= $signed(cfg_splitters[i*32 +: 32])) begin
                dest = dest + 1'b1;
            end
        end
    end

    // per-lane wires
    logic [NUM_PARTITIONS-1:0] lane_push_valid;
    logic [NUM_PARTITIONS-1:0] lane_push_ready;
    logic [NUM_PARTITIONS-1:0] lane_busy;

    logic all_idle;
    assign all_idle = (lane_busy == '0);

    assign input_fire = input_valid && input_ready;

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
                            cur_w0 <= input_data;
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

    genvar p;
    generate
        for (p = 0; p < NUM_PARTITIONS; p++) begin : g_lane
            dau_range_partition_lane u_lane (
                .clk(clk),
                .rst(rst),
                .push_valid(lane_push_valid[p]),
                .push_ready(lane_push_ready[p]),
                .push_w0(cur_w0),
                .push_w1(input_data),
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

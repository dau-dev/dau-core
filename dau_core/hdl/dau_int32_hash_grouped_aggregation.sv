`default_nettype none

`include "types.svh"

// Hash-mode grouped field-sum aggregation over 16-byte quad rows (two
// 64-bit words, w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th
// FIELD_WIDTH lane of the row, which at the default int32 is
// (key, value, aux0, aux1) order). One INT64 accumulator
// set per DISTINCT key in a CAPACITY-slot table, drained key-ascending at
// close-out — matches dau_core.operators.hash_grouped_field_sum_rows
// bit-for-bit.
//
// The difference from dau_int32_grouped_field_sum_aggregation is the whole
// point: the ordered tile groups CONSECUTIVE equal keys, so it needs the
// staged table pre-sorted on the group key and a reappearing key opens a
// second partial the host must merge. This tile holds per-key state, so
// input order does not matter and a key may reappear anywhere in the batch
// — one record per distinct key, always. That removes the sorted_by_key
// staging precondition from every grouped scan.
//
// Implementation: hash (low key bits) + linear probe over a CAPACITY-slot
// {sums[3:0], count, key} BRAM with a separate valid array (the keyed-state
// blueprint: the CLEAR sweep never touches the wide BRAM, and array access
// lives in a dedicated reset-free process so it infers block RAM). One row
// in flight — deliberately SEQUENTIAL, so the read-modify-write hazard a
// pipelined design must forward around does not exist. The declared ingest
// rate carries that honesty: five cycles per row in the collision-free case
// (w0, w1, probe, judge, accumulate).
//
// All four field sums accumulate unconditionally: the accumulate path is
// config-free, and cfg_field_mask selects only which sums the drain emits
// (the golden's contract — summing field 0 is legal and equals key*count).
//
// Two contracts the golden pins and this tile must honour:
//
//   * The drain is signed-key ASCENDING, by repeated min-extraction over
//     the valid slots. A hash table has no intrinsic order; leaking the
//     probe/slot order into the record stream would make results depend on
//     the hash function — unpinnable by any golden, unrelyable by any host.
//     Cost is O(CAPACITY x live groups), paid once per batch at close-out.
//
//   * Overflow is an ERROR, never an eviction. A row presenting a key not
//     in the table when the table is full raises GROUP_CAPACITY_EXCEEDED;
//     the batch is invalidated (group_count reads 0 and no success
//     close-out is raised), and the host's recovery is to split the batch
//     by key range and merge the partials — exact, because INT64 sums and
//     counts are additive per key (dau_core.operators.merge_hash_grouped).
//     Silently dropping or evicting a key would be wrong in a way no
//     downstream consumer could detect.
//
// The batch's group records form ONE record stream: output_last is set only
// on the final group's count word, and exactly one success close-out per
// batch follows it (the terminal tile convention). State is FRESH per
// batch: the CLEAR sweep runs from BOTH reset and close-out, so a mid-batch
// reset cannot leak one batch's keys into the next. A zero mask is an
// unconfigured tile (ERR_CONFIG); input_last on a w0 is a torn row
// (ERR_STREAM).
module dau_int32_hash_grouped_aggregation #(
    parameter int CAPACITY = 512,  // group slots; [1, 1024]
    // The ELEMENT TYPE of one field, as encoded by types.svh. The 128-bit row
    // is the transport unit and stays fixed; lane i is its i-th FIELD_BITS
    // lane, the decode row-unpack packs and the quad-row filter family reads.
    // This tile keeps the FIELDS lanes it names and ignores any a narrower
    // type leaves beyond them -- the lane count is what the four INT64
    // accumulators per slot cost, and it sizes the slot BRAM. `DAU_TYPE_INT32
    // is 32, so the default reproduces the four-field row port for port. The
    // sums are INT64 and the emitted key word is 64 bits whatever the element
    // is, which is why the type may narrow but not widen.
    parameter int FIELD_WIDTH = `DAU_TYPE_INT32
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [3:0]  cfg_field_mask,
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
    output logic [63:0]      group_count
);
    // these ARE dau_core.operators.HashGroupedErrorCode's values: the golden
    // decodes this wire, so a renumbering here is a contract break there
    localparam logic [7:0] ERR_CONFIG = 8'd2;          // BAD_FIELD_MASK
    localparam logic [7:0] ERR_GROUP_CAPACITY = 8'd3;  // GROUP_CAPACITY_EXCEEDED
    localparam logic [7:0] ERR_STREAM = 8'd4;          // BAD_STREAM (hardware only)
    localparam int AW = (CAPACITY <= 1) ? 1 : $clog2(CAPACITY);
    localparam int ROW_BITS = 128;  // the transport unit, fixed
    localparam int FIELDS   = 4;    // the lanes this tile sums
    localparam int FIELD_BITS      = `DAU_TYPE_WIDTH(FIELD_WIDTH);
    localparam int FIELD_IS_FLOAT  = `DAU_TYPE_IS_FLOAT(FIELD_WIDTH);
    localparam int FIELD_IS_SIGNED = `DAU_TYPE_IS_SIGNED(FIELD_WIDTH);
    localparam int LANE_BITS       = FIELDS * FIELD_BITS;  // the part of the row this tile reads
    // a per-group ROW COUNT, not an element: it counts rows, so it keeps its
    // own width whatever the summed values are
    localparam int COUNT_BITS = 32;
    // {sum3, sum2, sum1, sum0, count, key} -- the key is the only element in
    // the slot, so the BRAM narrows with the type and the sums do not
    localparam int SLOTW = FIELDS * 64 + COUNT_BITS + FIELD_BITS;

    if (CAPACITY < 1 || CAPACITY > 1024) begin : g_param_check
        $error("CAPACITY must be in [1, 1024]");
    end
    // The upper bound is the RECORD, not the lane: a sum word is 64 bits, so
    // an element wider than 32 would need more sum bits than the record can
    // express -- a versioned record, i.e. a different tile. Narrowing is free.
    if (FIELD_BITS < 8 || FIELD_BITS > 32 || (ROW_BITS % FIELD_BITS) != 0) begin : g_field_width_check
        $error("FIELD_WIDTH must divide 128 and lie in [8, 32]");
    end
    // A TYPE IS NOT A CAPABILITY. The drain ORDERS keys (signed-ascending
    // min-extraction) and each field is sign-extended into a two's-complement
    // INT64 accumulator: handed a float it would order exponent bits as
    // magnitude, and handed an unsigned type it would read every high-bit-set
    // value as negative.
    if (FIELD_IS_FLOAT != 0) begin : g_field_float_check
        $error("FIELD_WIDTH must not name a floating-point type: this tile orders and sums two's-complement integers");
    end
    if (FIELD_IS_SIGNED == 0) begin : g_field_signed_check
        $error("FIELD_WIDTH must not name an unsigned type: this tile orders and sums two's-complement integers");
    end

    typedef enum logic [3:0] {
        STATE_CLEAR,       // sweep the valid array (from reset and close-out)
        STATE_W0,          // accept w0; latch config on a batch's first beat
        STATE_W1,          // accept w1; the row is complete
        STATE_PROBE,       // present the probe index to the slot BRAM
        STATE_JUDGE,       // registered slot: match / empty / collide
        STATE_ACCUM,       // add into the latched base and write the slot
        STATE_DRAIN_SCAN,  // walk slots folding the running signed minimum
        STATE_DRAIN_READ,  // fold the final slot (one-cycle read lag)
        STATE_DRAIN_ADDR,  // present the winner's index
        STATE_DRAIN_JUDGE, // registered winner: latch its record
        STATE_EMIT,        // walk key word, mask-selected sums, count word
        STATE_CLOSE,       // final count word accepted -> success close-out
        STATE_DISCARD,     // error latched: consume the batch to input_last
        STATE_STATUS       // hold the close-out until accepted
    } state_t;

    state_t state;

    // slot payload; valid bits separate so the CLEAR sweep never touches the
    // wide BRAM (the scratchpad BRAM-inference lesson)
    (* ram_style = "block" *) logic [SLOTW-1:0] slots [0:CAPACITY-1];
    logic valid_bits [0:CAPACITY-1];

    logic        batch_open;
    logic [3:0]  active_mask;
    logic [31:0] live_count;
    logic [63:0] batch_groups;

    logic [63:0] row_w0;
    // the row being completed: w0 was registered, w1 is on the input, and the
    // lanes are read out of the assembled row rather than out of either word,
    // because at a narrower type a lane no longer lines up with a word
    logic [LANE_BITS-1:0] row_lanes;
    logic [FIELD_BITS-1:0] cur_key;
    logic [FIELD_BITS-1:0] cur_fields [FIELDS];
    logic        cur_last;

    logic [AW-1:0] probe_idx;
    logic [31:0]   probes_done;
    logic [AW-1:0] clear_idx;
    logic          err_pending;
    logic [7:0]    err_code;

    // drain bookkeeping
    logic [AW-1:0] scan_idx;
    logic          scan_slot_valid;
    logic          min_found;
    logic [FIELD_BITS-1:0] min_key;
    logic [AW-1:0] min_idx;

    // emit registers
    logic [FIELD_BITS-1:0] emit_key;
    logic [63:0] emit_sums [FIELDS];
    logic [63:0] emit_count;
    logic [2:0]  emit_phase;  // 0 = key word, 1..4 walk the fields, 5 = count
    logic        emit_final;

    logic input_fire;
    logic output_fire;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign input_ready = (state == STATE_W0) || (state == STATE_W1) || (state == STATE_DISCARD);
    assign row_lanes = LANE_BITS'({input_data, row_w0});

    // slot BRAM: dedicated reset-free process (BRAM inference). The read
    // presents in PROBE/DRAIN_ADDR and lands one cycle later for JUDGE.
    logic [SLOTW-1:0] slot_q;
    logic             slot_we;
    logic [AW-1:0]    slot_addr;
    logic [SLOTW-1:0] slot_wdata;
    always_ff @(posedge clk) begin
        if (slot_we) begin
            slots[slot_addr] <= slot_wdata;
        end
        slot_q <= slots[slot_addr];
    end

    // judged outcomes (combinational off the registered slot read)
    logic valid_q;
    logic hit;
    logic empty;
    assign hit = valid_q && (slot_q[0 +: FIELD_BITS] == cur_key);
    assign empty = !valid_q;

    // The accumulate is split across two cycles, and the split is why this
    // tile closes timing. Selecting the base (a 32-bit key compare feeding a
    // 64-bit mux) and adding into it (four 64-bit carry chains) each fit in a
    // cycle; chained into the same one they do not, and the BRAM write data
    // arrives late enough to miss. JUDGE latches the base, ACCUM adds and
    // writes. The declared ingest rate carries the extra cycle.
    logic [63:0] base_sums [FIELDS];
    logic [COUNT_BITS-1:0] base_count;

    // all four sums accumulate unconditionally: register -> add -> BRAM
    logic [63:0] next_sums [FIELDS];
    always_comb begin
        for (int i = 0; i < FIELDS; i++) begin
            // sign-extend the field before the INT64 accumulate
            next_sums[i] = base_sums[i] + 64'(signed'(cur_fields[i]));
        end
    end

    assign slot_addr = (state == STATE_DRAIN_SCAN) ? scan_idx
                     : ((state == STATE_DRAIN_READ) || (state == STATE_DRAIN_ADDR)) ? min_idx
                     : probe_idx;
    assign slot_wdata = {next_sums[3], next_sums[2], next_sums[1], next_sums[0], base_count + COUNT_BITS'(1), cur_key};
    assign slot_we = (state == STATE_ACCUM);

    task automatic reset_state();
        state <= STATE_CLEAR;
        clear_idx <= '0;
        batch_open <= 1'b0;
        active_mask <= 4'd0;
        live_count <= 32'd0;
        batch_groups <= 64'd0;
        group_count <= 64'd0;
        output_valid <= 1'b0;
        output_data <= 64'd0;
        output_last <= 1'b0;
        status_valid <= 1'b0;
        status_error <= 1'b0;
        status_error_code <= 8'd0;
        err_pending <= 1'b0;
        err_code <= 8'd0;
        row_w0 <= 64'd0;
        cur_key <= '0;
        cur_last <= 1'b0;
        probe_idx <= '0;
        probes_done <= 32'd0;
        valid_q <= 1'b0;
        scan_idx <= '0;
        scan_slot_valid <= 1'b0;
        min_found <= 1'b0;
        min_key <= '0;
        min_idx <= '0;
        emit_key <= '0;
        emit_count <= 64'd0;
        emit_phase <= 3'd0;
        emit_final <= 1'b0;
        base_count <= '0;
        for (int i = 0; i < FIELDS; i++) begin
            cur_fields[i] <= '0;
            emit_sums[i] <= 64'd0;
            base_sums[i] <= 64'd0;
        end
    endtask

    task automatic raise_error(input logic [7:0] code);
        err_pending <= 1'b1;
        err_code <= code;
        state <= STATE_STATUS;
    endtask

    // enter the drain: rewind the scan and the running minimum
    task automatic start_drain();
        scan_idx <= '0;
        scan_slot_valid <= 1'b0;
        min_found <= 1'b0;
        state <= STATE_DRAIN_SCAN;
    endtask

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_CLEAR: begin
                    valid_bits[clear_idx] <= 1'b0;
                    if (clear_idx == AW'(CAPACITY - 1)) begin
                        clear_idx <= '0;
                        live_count <= 32'd0;
                        state <= STATE_W0;
                    end else begin
                        clear_idx <= clear_idx + 1'b1;
                    end
                end
                STATE_W0: begin
                    if (input_fire) begin
                        if (!batch_open) begin
                            batch_open <= 1'b1;
                            active_mask <= cfg_field_mask;
                            batch_groups <= 64'd0;
                        end
                        if (!batch_open && (cfg_field_mask == 4'd0)) begin
                            // unconfigured: latch CONFIG now, then drain the
                            // batch (STATE_DISCARD reports whatever is latched,
                            // so the error must be set at entry, not there)
                            if (input_last) begin
                                raise_error(ERR_CONFIG);
                            end else begin
                                err_pending <= 1'b1;
                                err_code <= ERR_CONFIG;
                                state <= STATE_DISCARD;
                            end
                        end else if (input_last) begin
                            raise_error(ERR_STREAM);  // torn row
                        end else begin
                            row_w0 <= input_data;
                            state <= STATE_W1;
                        end
                    end
                end
                STATE_W1: begin
                    if (input_fire) begin
                        cur_key <= row_lanes[0*FIELD_BITS +: FIELD_BITS];
                        cur_fields[0] <= row_lanes[0*FIELD_BITS +: FIELD_BITS];
                        cur_fields[1] <= row_lanes[1*FIELD_BITS +: FIELD_BITS];
                        cur_fields[2] <= row_lanes[2*FIELD_BITS +: FIELD_BITS];
                        cur_fields[3] <= row_lanes[3*FIELD_BITS +: FIELD_BITS];
                        cur_last <= input_last;
                        // the hash: the key ZERO-EXTENDED to 32 bits, so the modulo runs at the
                        // container's width and CAPACITY is never truncated into it
                        probe_idx <= AW'(32'(row_lanes[0*FIELD_BITS +: FIELD_BITS]) % CAPACITY);
                        probes_done <= 32'd0;
                        state <= STATE_PROBE;
                    end
                end
                STATE_PROBE: begin
                    // slot read presented this cycle; judged next. The valid
                    // bit registers alongside so JUDGE sees a coherent pair.
                    valid_q <= valid_bits[probe_idx];
                    state <= STATE_JUDGE;
                end
                STATE_JUDGE: begin
                    // latch the base this row accumulates onto; ACCUM adds it
                    // and writes. A miss into a free slot starts from zero.
                    base_count <= hit ? slot_q[FIELD_BITS +: COUNT_BITS] : '0;
                    for (int i = 0; i < FIELDS; i++) begin
                        base_sums[i] <= hit ? slot_q[FIELD_BITS + COUNT_BITS + i * 64 +: 64] : 64'd0;
                    end
                    if (hit) begin
                        state <= STATE_ACCUM;
                    end else if (empty) begin
                        if (live_count < CAPACITY[31:0]) begin
                            valid_bits[probe_idx] <= 1'b1;
                            live_count <= live_count + 32'd1;
                            state <= STATE_ACCUM;
                        end else begin
                            // the table is full and this key is new: refuse
                            // the batch rather than evict or drop
                            err_pending <= 1'b1;
                            err_code <= ERR_GROUP_CAPACITY;
                            state <= cur_last ? STATE_STATUS : STATE_DISCARD;
                        end
                    end else begin
                        // collision: advance the probe (wrapping). A full scan
                        // without a hit or an empty slot means the table holds
                        // CAPACITY other keys.
                        if (probes_done + 32'd1 >= CAPACITY[31:0]) begin
                            err_pending <= 1'b1;
                            err_code <= ERR_GROUP_CAPACITY;
                            state <= cur_last ? STATE_STATUS : STATE_DISCARD;
                        end else begin
                            probes_done <= probes_done + 32'd1;
                            probe_idx <= (probe_idx == AW'(CAPACITY - 1)) ? '0 : probe_idx + 1'b1;
                            state <= STATE_PROBE;
                        end
                    end
                end
                STATE_ACCUM: begin
                    // slot_we writes {next_sums, base_count + 1, cur_key} at
                    // slot_addr = probe_idx, which JUDGE left settled. If the
                    // drain starts on this edge the write still commits first,
                    // so DRAIN_SCAN reads the updated slot.
                    if (cur_last) begin
                        start_drain();
                    end else begin
                        state <= STATE_W0;
                    end
                end
                STATE_DRAIN_SCAN: begin
                    // slot_addr = scan_idx; slot_q lags one cycle, so this
                    // cycle folds the PREVIOUS slot's {valid, key} pair
                    scan_slot_valid <= valid_bits[scan_idx];
                    if (scan_idx != '0) begin
                        if (scan_slot_valid && (!min_found || ($signed(slot_q[0 +: FIELD_BITS]) < $signed(min_key)))) begin
                            min_found <= 1'b1;
                            min_key <= slot_q[0 +: FIELD_BITS];
                            min_idx <= scan_idx - 1'b1;
                        end
                    end
                    if (scan_idx == AW'(CAPACITY - 1)) begin
                        state <= STATE_DRAIN_READ;
                    end
                    scan_idx <= scan_idx + 1'b1;
                end
                STATE_DRAIN_READ: begin
                    // fold the final slot (registered on the last scan cycle)
                    if (scan_slot_valid && (!min_found || ($signed(slot_q[0 +: FIELD_BITS]) < $signed(min_key)))) begin
                        min_found <= 1'b1;
                        min_key <= slot_q[0 +: FIELD_BITS];
                        min_idx <= AW'(CAPACITY - 1);
                        state <= STATE_DRAIN_ADDR;
                    end else begin
                        // min_found is guaranteed here: the drain is entered
                        // only after a row was absorbed, so at least one slot
                        // is live and every re-entry follows an extraction
                        // that left live_count > 0
                        state <= STATE_DRAIN_ADDR;
                    end
                end
                STATE_DRAIN_ADDR: begin
                    // min_idx settled last cycle; slot_addr presents it now and
                    // slot_q holds the winner one cycle later
                    state <= STATE_DRAIN_JUDGE;
                end
                STATE_DRAIN_JUDGE: begin
                    // slot_q now holds the winner (slot_addr = min_idx)
                    emit_key <= slot_q[0 +: FIELD_BITS];
                    emit_count <= {32'd0, slot_q[FIELD_BITS +: COUNT_BITS]};
                    for (int i = 0; i < FIELDS; i++) begin
                        emit_sums[i] <= slot_q[FIELD_BITS + COUNT_BITS + i * 64 +: 64];
                    end
                    valid_bits[min_idx] <= 1'b0;  // extracted: never re-selected
                    live_count <= live_count - 32'd1;
                    emit_phase <= 3'd0;
                    emit_final <= (live_count == 32'd1);  // this is the last group
                    state <= STATE_EMIT;
                end
                STATE_EMIT: begin
                    if (!output_valid || output_ready) begin
                        if (emit_phase == 3'd0) begin
                            output_valid <= 1'b1;
                            output_data <= 64'(signed'(emit_key));
                            output_last <= 1'b0;
                            emit_phase <= 3'd1;
                        end else if (emit_phase < 3'd5) begin
                            if (active_mask[emit_phase[1:0] - 2'd1]) begin
                                output_valid <= 1'b1;
                                output_data <= emit_sums[emit_phase[1:0] - 2'd1];
                                output_last <= 1'b0;
                            end
                            emit_phase <= emit_phase + 3'd1;
                        end else begin
                            // the count word closes the group record
                            output_valid <= 1'b1;
                            output_data <= emit_count;
                            output_last <= emit_final;
                            batch_groups <= batch_groups + 64'd1;
                            if (emit_final) begin
                                state <= STATE_CLOSE;
                            end else begin
                                start_drain();  // extract the next-smallest key
                            end
                        end
                    end
                end
                STATE_CLOSE: begin
                    // success close-out only after the final count word is
                    // accepted (the record writer waits on a consumed status)
                    if (!output_valid || output_ready) begin
                        group_count <= batch_groups;
                        batch_open <= 1'b0;
                        status_valid <= 1'b1;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= STATE_STATUS;
                    end
                end
                STATE_DISCARD: begin
                    if (input_fire && input_last) begin
                        state <= STATE_STATUS;
                    end
                end
                STATE_STATUS: begin
                    if (err_pending && !status_valid) begin
                        status_valid <= 1'b1;
                        status_error <= 1'b1;
                        status_error_code <= err_code;
                        group_count <= 64'd0;  // an error invalidates the batch
                    end
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        err_pending <= 1'b0;
                        batch_open <= 1'b0;
                        batch_groups <= 64'd0;
                        clear_idx <= '0;
                        state <= STATE_CLEAR;  // fresh state per batch
                    end
                end
                default: state <= STATE_CLEAR;
            endcase
        end
    end
endmodule
`default_nettype wire

`default_nettype none

`include "types.svh"

// Wide-lane batch field-sum aggregation: SLOTS whole 16-byte quad rows per
// beat, one beat per cycle — the multi-row-per-cycle terminal of the wide
// lane (ingest SLOTS rows/cycle vs the 64-bit tile's one per two). A slot
// carries one row {aux1, aux0, value, key} (field i at bits
// [FIELD_BITS*i +: FIELD_BITS]);
// the reserved ALL-ONES 128-bit row is the PAD (the ragged-tail / dropped-
// slot convention shared with the wide dispatcher and the wide filter):
// pads are neither accumulated nor counted, so an upstream wide predicate
// filter drops a row by replacing its slot with the pad — no compaction.
//
// Semantics are dau_core.operators.field_sum_rows, identical to the 64-bit
// tile: one signed 64-bit sum per cfg_field_mask-selected field plus the
// row count, emitted as one record at input_last (sums ascending, then the
// count word with output_last), then the success close-out. The mask
// latches at the first beat; a zero mask drains the batch and reports
// ERR_CONFIG. Torn rows cannot exist (a beat is whole rows), so the 64-bit
// tile's ERR_STREAM path has no equivalent here. An all-pad beat (a
// trailing pad-only tail) accumulates nothing and closes normally.
module dau_wide_field_sum_aggregation #(
    parameter int SLOTS = 4,  // quad rows per beat: 1, 2, 4, or 8
    // The ELEMENT TYPE of one field, as encoded by types.svh. A slot stays a
    // 128-bit row (the wide lane's transport unit, and what the PAD is a value
    // of), and field i is its i-th FIELD_BITS lane. This tile sums the FIELDS
    // lanes it names and ignores any a narrower type leaves beyond them.
    // `DAU_TYPE_INT32 is 32, so the default reproduces the four-field slot
    // port for port; the sums are INT64 whatever the element is, which is why
    // the type may narrow but not widen.
    parameter int FIELD_WIDTH = `DAU_TYPE_INT32
) (
    input  wire logic                     clk,
    input  wire logic                     rst,
    input  wire logic [3:0]               cfg_field_mask,
    input  wire logic                     input_valid,
    output logic                          input_ready,
    input  wire logic [SLOTS*128-1:0]     input_data,
    input  wire logic                     input_last,
    output logic                          output_valid,
    input  wire logic                     output_ready,
    output logic [63:0]                   output_data,
    output logic                          output_last,
    output logic                          status_valid,
    input  wire logic                     status_ready,
    output logic                          status_error,
    output logic [7:0]                    status_error_code,
    output logic [63:0]                   aggregated_count
);
    localparam int ROW_BITS = 128;  // one slot, the wide lane's transport unit
    localparam int FIELDS   = 4;    // the lanes this tile accumulates
    localparam int FIELD_BITS      = `DAU_TYPE_WIDTH(FIELD_WIDTH);
    localparam int FIELD_IS_FLOAT  = `DAU_TYPE_IS_FLOAT(FIELD_WIDTH);
    localparam int FIELD_IS_SIGNED = `DAU_TYPE_IS_SIGNED(FIELD_WIDTH);

    if (!(SLOTS == 1 || SLOTS == 2 || SLOTS == 4 || SLOTS == 8)) begin : g_bad_slots
        $error("SLOTS must be 1, 2, 4, or 8");
    end
    // The upper bound is the RECORD, not the lane: a sum word is 64 bits, so
    // an element wider than 32 would need more sum bits than the record can
    // express -- a versioned record, i.e. a different tile. Narrowing is free.
    if (FIELD_BITS < 8 || FIELD_BITS > 32 || (ROW_BITS % FIELD_BITS) != 0) begin : g_field_width_check
        $error("FIELD_WIDTH must divide 128 and lie in [8, 32]");
    end
    // A TYPE IS NOT A CAPABILITY. The reduction tree sign-extends each field
    // and the accumulator is two's-complement INT64: a float would be added as
    // exponent bits, an unsigned type read as negative above its half-range.
    if (FIELD_IS_FLOAT != 0) begin : g_field_float_check
        $error("FIELD_WIDTH must not name a floating-point type: this tile sums two's-complement integers");
    end
    if (FIELD_IS_SIGNED == 0) begin : g_field_signed_check
        $error("FIELD_WIDTH must not name an unsigned type: this tile sign-extends into a signed accumulator");
    end

    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [ROW_BITS-1:0] PAD_ROW = {ROW_BITS{1'b1}};

    typedef enum logic [2:0] {
        STATE_ACCUM,   // accept one beat of SLOTS rows per cycle
        STATE_EMIT,    // walk the selected sums, then the count word
        STATE_CLOSE,   // count word accepted -> raise the success close-out
        STATE_DISCARD, // bad config: consume the batch to input_last
        STATE_STATUS   // hold a close-out (success or error) until accepted
    } state_t;

    state_t state;

    logic        batch_open;
    logic [3:0]  active_mask;
    logic signed [63:0] sums [FIELDS];
    logic [63:0] row_count;
    logic [2:0]  emit_phase;

    logic input_fire;
    logic output_fire;
    logic first_beat;
    logic [3:0] eff_mask;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    // While the final beat of a batch is in the pipeline the next batch's
    // first beat must not be accepted, or it would latch its mask over a
    // batch that has not finished accumulating. One bubble per batch.
    assign input_ready = (state == STATE_ACCUM && !(pend_valid && pend_last)) || (state == STATE_DISCARD);

    assign first_beat = !batch_open;
    assign eff_mask = first_beat ? cfg_field_mask : active_mask;

    // Per-beat combinational reduction: for each field, a BALANCED adder tree
    // over non-pad slots, plus the non-pad slot count.
    //
    // Two things here are deliberate and were not always true. The tree is
    // balanced -- (a+b)+(c+d), depth ceil(log2(SLOTS)) -- rather than a
    // left-fold accumulation, whose depth is SLOTS. And it is BEAT_SUM_W wide,
    // not 64: summing SLOTS signed FIELD_BITS values needs
    // FIELD_BITS+clog2(SLOTS) bits, and only the running accumulator below
    // needs the full 64. A left-fold at
    // 64 bits put four chained 64-bit carry chains ahead of the accumulator and
    // made this tile the path that bound the whole job clock.
    localparam int BEAT_SUM_W = FIELD_BITS + $clog2(SLOTS);
    localparam int ROWS_W = $clog2(SLOTS + 1);

    // reduction depth, so the tree stays balanced at every SLOTS. At SLOTS==1
    // this is 0: there is nothing to reduce and the single slot IS the sum.
    localparam int LEVELS = $clog2(SLOTS);

    logic [SLOTS-1:0] slot_live;
    logic signed [BEAT_SUM_W-1:0] slot_term [0:FIELDS-1][0:SLOTS-1]; // [field][slot], 0 for pads
    // [field][level][node]: level 0 is the slot terms, each level halves, and
    // level LEVELS node 0 is the beat sum. Written as a parameterized tree
    // rather than the hand-written (a+b)+(c+d) it replaces, which silently
    // fixed the tile at four slots.
    logic signed [BEAT_SUM_W-1:0] tree [0:FIELDS-1][0:LEVELS][0:SLOTS-1];
    logic signed [BEAT_SUM_W-1:0] beat_sum [FIELDS];
    logic [ROWS_W-1:0] beat_rows;

    // RETIME (2026-08-11): the reduction tree and the 64-bit accumulate used
    // to share one cycle, and at SLOTS=8 the composed 150 MHz path ran the
    // map ALU through slot extraction, three levels of 35-bit adds and a
    // 64-bit accumulate as ONE 29-deep carry chain -- WNS -0.665. These
    // registers split it: the tree lands here, the accumulate reads it next
    // cycle. A pipeline register, not a stall, so the beat rate is unchanged.
    logic                          pend_valid;
    logic                          pend_first;
    logic                          pend_last;
    logic signed [BEAT_SUM_W-1:0]  pend_sum [FIELDS];
    logic [ROWS_W-1:0]             pend_rows;

    always_comb begin
        slot_live = '0;
        for (int f = 0; f < FIELDS; f++) begin
            for (int s = 0; s < SLOTS; s++) begin
                slot_term[f][s] = '0;
            end
        end
        for (int s = 0; s < SLOTS; s++) begin
            slot_live[s] = (input_data[ROW_BITS*s +: ROW_BITS] != PAD_ROW);
            for (int f = 0; f < FIELDS; f++) begin
                if (slot_live[s]) begin
                    slot_term[f][s] = BEAT_SUM_W'(signed'(input_data[ROW_BITS*s + FIELD_BITS*f +: FIELD_BITS]));
                end
            end
        end
        beat_rows = ROWS_W'($countones(slot_live));
        for (int f = 0; f < FIELDS; f++) begin
            for (int s = 0; s < SLOTS; s++) begin
                tree[f][0][s] = slot_term[f][s];
            end
            for (int l = 0; l < LEVELS; l++) begin
                for (int i = 0; i < SLOTS; i++) begin
                    tree[f][l + 1][i] = (i < (SLOTS >> (l + 1)))
                        ? (tree[f][l][2 * i] + tree[f][l][2 * i + 1])
                        : '0;
                end
            end
            beat_sum[f] = tree[f][LEVELS][0];
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_ACCUM: begin
                    // STAGE 2 FIRST, and the order is load-bearing. Both
                    // stages assign pend_valid in this one always_ff, so on
                    // back-to-back beats the LATER assignment wins: with
                    // stage 1 first, stage 2's clear silently discarded the
                    // beat stage 1 had just accepted (counts came out about
                    // half, and the close-out never fired). Written this way
                    // stage 1's set wins when a beat arrives and stage 2's
                    // clear applies only when one does not.
                    //
                    // It accumulates the beat registered LAST cycle, so it
                    // runs whether or not a new beat arrives -- which is what
                    // lets the final beat land after input_last was accepted.
                    if (pend_valid) begin
                        if (pend_first) begin
                            for (int f = 0; f < FIELDS; f++) begin
                                sums[f] <= (active_mask[f]) ? 64'(pend_sum[f]) : 64'sd0;
                            end
                            row_count <= pend_rows;
                        end else begin
                            for (int f = 0; f < FIELDS; f++) begin
                                if (active_mask[f]) begin
                                    sums[f] <= sums[f] + 64'(pend_sum[f]);
                                end
                            end
                            row_count <= row_count + pend_rows;
                        end
                        pend_valid <= 1'b0;
                        if (pend_last) begin
                            emit_phase <= 3'd0;
                            state <= STATE_EMIT;
                        end
                    end
                    // STAGE 1: latch the mask, hand the reduction to the pipe
                    if (input_fire) begin
                        if (first_beat && (cfg_field_mask == 4'd0)) begin
                            if (input_last) begin
                                raise_error(ERR_CONFIG);
                            end else begin
                                batch_open <= 1'b1;
                                state <= STATE_DISCARD;
                            end
                        end else begin
                            // STAGE 1: latch the batch mask and hand the
                            // reduction to the pipeline register. No adding
                            // here -- that is the whole point of the retime.
                            if (first_beat) begin
                                batch_open <= 1'b1;
                                active_mask <= cfg_field_mask;
                            end
                            pend_valid <= 1'b1;
                            pend_first <= first_beat;
                            pend_last <= input_last;
                            for (int f = 0; f < FIELDS; f++) begin
                                pend_sum[f] <= beat_sum[f];
                            end
                            pend_rows <= beat_rows;
                        end
                    end
                end
                STATE_EMIT: begin
                    if (!output_valid || output_ready) begin
                        if (emit_phase < 3'd4) begin
                            if (active_mask[emit_phase[1:0]]) begin
                                output_valid <= 1'b1;
                                output_data <= unsigned'(sums[emit_phase[1:0]]);
                                output_last <= 1'b0;
                            end
                            emit_phase <= emit_phase + 3'd1;
                        end else begin
                            output_valid <= 1'b1;
                            output_data <= row_count;
                            output_last <= 1'b1;
                            state <= STATE_CLOSE;
                        end
                    end
                end
                STATE_CLOSE: begin
                    if (!output_valid || output_ready) begin
                        aggregated_count <= row_count;
                        batch_open <= 1'b0;
                        status_valid <= 1'b1;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= STATE_STATUS;
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
                        state <= STATE_ACCUM;
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
            aggregated_count <= 64'd0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
            // a rejected batch must not leave a beat in the pipeline for the
            // next batch to accumulate
            clear_pending();
        end
    endtask

    task automatic clear_pending;
        begin
            pend_valid <= 1'b0;
            pend_first <= 1'b0;
            pend_last <= 1'b0;
            pend_rows <= '0;
            for (int f = 0; f < FIELDS; f++) begin
                pend_sum[f] <= '0;
            end
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_ACCUM;
            batch_open <= 1'b0;
            active_mask <= 4'd0;
            for (int f = 0; f < FIELDS; f++) begin
                sums[f] <= 64'sd0;
            end
            row_count <= 64'd0;
            emit_phase <= 3'd0;
            output_valid <= 1'b0;
            output_data <= 64'd0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            aggregated_count <= 64'd0;
            clear_pending();
        end
    endtask
endmodule

`default_nettype wire

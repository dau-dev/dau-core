`default_nettype none

// Row-unpack front-end feed tile: widens a stream of PACKED rows into the
// standard 16-byte int32 quad rows the operator tiles consume. Each input
// word carries one complete packed row (<=64 bits of the 64-bit word); the
// tile bit-extracts four configured fields, sign- or zero-extends each to
// int32, and emits the 16-byte quad row as two 64-bit words
// (w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th 32-bit lane in
// (key, value, aux0, aux1) order — field 0 = key = w0.lo — byte-identical to
// what the pair-key partition / row filters / row map / field-sum tiles
// expect). One input word in, two output words out, order and count
// preserved. dau_core.operators.unpack_rows is the golden.
//
// Why this tile exists: the dpv1 scan feed is DDR-BYTE-bandwidth-bound
// (~970 MB/s profiled). Staging rows PACKED to DDR (fewer bytes/row) and
// widening them on-chip here ~doubles rows/sec off DDR AND halves the host
// DMA, with the operators downstream unchanged. This MVP covers packed rows
// up to 64 bits (q6 at 59 bits, q1 at 63 bits).
//
// Each output field i is bits packed[cfg_field{i}_offset +: cfg_field{i}_width]
// (offset 0..63, width 1..32) sign-extended when cfg_field{i}_signed else
// zero-extended. A field descriptor with width==0, width>32, or
// offset+width>64 is invalid: config precedence (batch-latched at the first
// row) drains the batch, then reports ERR_CONFIG.
//
// BYPASS mode (cfg_bypass, batch-latched at the first row like the field
// descriptors): when set, the input is already the 16-byte int32 quad row —
// TWO 64-bit words per row, batch-last on the row's second word — and the tile
// forwards each word to the output byte-identically with ZERO added latency
// (a true input->output wire, not widen-then-renarrow), so the bypass baseline
// measures the operators-only path with the host packing removed. The field
// descriptors are unused in bypass (not required, not checked). This lets ONE
// bitstream benchmark packed (cfg_bypass=0) vs unpacked (cfg_bypass=1) rows on
// the same silicon, isolating the host packing overhead. The passthrough is
// gated so a prior unpack batch's still-draining output tail is never
// overtaken across a mode change (the emit-drain re-arm gate holds in both
// modes). Because a bypass row is two words, input_last is only legal on a
// row's w1 (odd word); input_last on a w0 is a torn row that invalidates the
// batch and reports ERR_STREAM — the quad-row-tile convention (pair-key
// filter, row filter/map, field-sum), never emitting a w0 tagged last.
//
// A composed-pipeline front-end stage: silent on success (the downstream
// tile's close-out is authoritative — success_close_out=False), raising its
// own status only for an invalid config (ERR_CONFIG, unpack) or a torn bypass
// row (ERR_STREAM). Unpack framing cannot tear (one input word is always one
// complete packed row); only bypass's two-word framing can. The 2-beat unpack
// output runs through a decoupled emit engine with a drain gate (S_ERR_DRAIN)
// so a following batch's ERR_CONFIG can never overtake this batch's un-drained
// final output beat.
//
// OUT_WIDTH (the rows/cycle axis): 64 (default) emits each quad row as two
// 64-bit beats — the classic quad-stream framing, byte-identical to the
// pre-parameter tile. 128 emits ONE whole quad row per beat ({w1, w0}: bits
// [63:0] = {value, key}, [127:64] = {aux1, aux0}), so unpack sustains one
// packed word in / one quad row out PER CYCLE — the shared-front emit cap
// (2 cycles/quad at 64) that silicon showed gates the packed feed is lifted
// without touching the lane operators. In 128-bit bypass the two-word quad
// input is gearboxed into one output beat (w0 latched, the row emitted on its
// w1 cycle — still zero added row latency); torn-row and batch-latch semantics
// are unchanged. Any other OUT_WIDTH fails elaboration.
//
// 256 and 512 carry SLOTS = OUT_WIDTH/128 whole quad rows per beat, slot s at
// [128*s +: 128], and widen the INPUT to match: IN_WIDTH = SLOTS*64, packed
// row s at [64*s +: 64]. This is what lets a packed feed drive a SLOTS-wide
// lane — at OUT_WIDTH=128 the tile emits one row per beat and starves a
// four-slot wide-row chain, which is why q6-packed-wide and q6-wide-sf1 could
// not be composed. The field extraction is replicated per slot and is purely
// combinational, so the cost is linear in SLOTS with no new sequencing.
//
// RAGGED TAILS (SLOTS > 1 only): a batch whose row count is not a multiple of
// SLOTS is padded by the host with the reserved ALL-ONES packed word, which
// this tile passes through as the reserved ALL-ONES 128-bit PAD row rather
// than unpacking it — the same PAD the wide-row tiles already drop in fabric,
// so positions are preserved and no consumer changes. At SLOTS == 1 every
// beat is exactly one row, raggedness cannot arise, and an all-ones packed
// word unpacks normally exactly as the silicon-proven 64/128 builds do.
//
// BYPASS at any width needs TWO input beats per output beat, because a quad
// row is 128 bits against a 64-bit packed row: 2*IN_WIDTH == OUT_WIDTH holds
// for every SLOTS, so the existing gearbox (beat 0 latched, the beat emitted
// on beat 1) generalizes unchanged.
//
// FOLLOW-UP (not wired here): realizing the feed win requires (a) the scan
// composition to stage packed rows and place this unpacker after the burst
// reader, before the broadcast/partition fan-out, and (b) a flywheel
// pack-schema deriving each field's offset/width from the column ranges.
module dau_int32_row_unpack #(
    parameter int unsigned OUT_WIDTH = 64
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [5:0]  cfg_field0_offset,
    input  wire logic [5:0]  cfg_field0_width,
    input  wire logic        cfg_field0_signed,
    input  wire logic [5:0]  cfg_field1_offset,
    input  wire logic [5:0]  cfg_field1_width,
    input  wire logic        cfg_field1_signed,
    input  wire logic [5:0]  cfg_field2_offset,
    input  wire logic [5:0]  cfg_field2_width,
    input  wire logic        cfg_field2_signed,
    input  wire logic [5:0]  cfg_field3_offset,
    input  wire logic [5:0]  cfg_field3_width,
    input  wire logic        cfg_field3_signed,
    input  wire logic        cfg_bypass,
    input  wire logic        input_valid,
    output logic             input_ready,
    input  wire logic [((OUT_WIDTH >= 128) ? (OUT_WIDTH / 128) : 1) * 64 - 1:0] input_data,
    input  wire logic        input_last,
    output logic             output_valid,
    input  wire logic        output_ready,
    output logic [OUT_WIDTH-1:0] output_data,
    output logic             output_last,
    output logic             status_valid,
    input  wire logic        status_ready,
    output logic             status_error,
    output logic [7:0]       status_error_code
);
    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;

    // rows/cycle axis: WIDE emits SLOTS whole quad rows per beat (EMIT_W0 is
    // the beat's only — and final — beat; EMIT_W1 is unreachable)
    localparam bit WIDE = (OUT_WIDTH >= 128);
    // quad rows per output beat, and the packed rows per input beat that feed
    // them one-for-one
    localparam int unsigned SLOTS = WIDE ? (OUT_WIDTH / 128) : 1;
    localparam int unsigned IN_WIDTH = SLOTS * 64;
    // the reserved ragged-tail pads: an all-ones packed row passes through as
    // the all-ones quad row the wide-row tiles already drop (SLOTS > 1 only)
    localparam logic [63:0]  PAD_PACKED = {64{1'b1}};
    localparam logic [127:0] PAD_ROW    = {128{1'b1}};
    if (OUT_WIDTH != 64 && OUT_WIDTH != 128 && OUT_WIDTH != 256 && OUT_WIDTH != 512) begin : g_bad_out_width
        $fatal(1, "dau_int32_row_unpack: OUT_WIDTH must be 64, 128, 256, or 512");
    end

    typedef enum logic [1:0] {
        S_RUN,       // accept packed rows (batch config latches on the first)
        S_DISCARD,   // bad config: consume the batch to input_last
        S_ERR_DRAIN, // hold input closed until the in-flight output beat drains
        S_STATUS     // hold the ERR_CONFIG close-out until accepted
    } state_t;

    typedef enum logic [1:0] {
        EMIT_IDLE,
        EMIT_W0,
        EMIT_W1
    } emit_t;

    state_t state;
    emit_t  emit_state;

    // batch-latched field descriptors + mode (quasi-static cfg inputs sampled
    // at the first row so a mid-batch change cannot re-slice the packed rows or
    // flip the framing)
    logic       batch_open;
    logic       active_bypass;
    // bypass two-word row phase: 0 = the next accepted word starts a row (w0),
    // 1 = the next completes it (w1). input_last is only legal at phase 1.
    logic       bypass_phase;
    logic [5:0] active_offset0, active_offset1, active_offset2, active_offset3;
    logic [5:0] active_width0, active_width1, active_width2, active_width3;
    logic       active_signed0, active_signed1, active_signed2, active_signed3;

    // one held quad row per slot; at SLOTS == 1 this is the classic
    // {w1, w0} pair, so the 64-bit two-beat path reads its halves
    logic [127:0] emit_row [SLOTS];
    logic         emit_last;

    logic input_fire;
    logic output_fire;
    logic emit_final;
    logic emit_free;
    logic emit_busy;
    logic bypass_pass;
    logic first_row;
    logic config_ok;
    logic eff_bypass;

    logic [5:0] eff_offset0, eff_offset1, eff_offset2, eff_offset3;
    logic [5:0] eff_width0, eff_width1, eff_width2, eff_width3;
    logic       eff_signed0, eff_signed1, eff_signed2, eff_signed3;

    // the widened quad row per input slot, combinational
    logic [127:0] row_out [SLOTS];

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    // the emit engine frees on the accepted final beat of a row (its second
    // 64-bit beat, or its single 128-bit beat when WIDE)
    assign emit_final = WIDE ? (emit_state == EMIT_W0) : (emit_state == EMIT_W1);
    assign emit_free = (emit_state == EMIT_IDLE) || (emit_final && output_fire);
    assign emit_busy = emit_state != EMIT_IDLE;

    // BYPASS passthrough is a true input->output wire (zero added latency),
    // engaged only in a bypass batch and only while the unpack emit engine is
    // idle so a prior unpack batch's in-flight tail is never overtaken across a
    // mode change
    assign bypass_pass = eff_bypass && (state == S_RUN) && !emit_busy;

    // UNPACK (S_RUN): loading the emit engine needs it free (or freeing) that
    // cycle. BYPASS (S_RUN): a passthrough word waits on the downstream, but a
    // torn w0 is accepted-and-dropped regardless of output_ready (it is not
    // forwarded, so gating it on the sink would deadlock). Held off until any
    // prior unpack tail has drained. S_DISCARD drains a bad-config batch
    // (unpack only). The check depends only on input_last framing, never the
    // payload.
    if (WIDE) begin : g_out_wide
        // 128-bit quad stream: the emit engine holds one whole row per beat.
        // BYPASS gearboxes the two-word quad input: a w0 (phase 0) is latched
        // without touching the sink (a torn w0 — input_last at phase 0 — is
        // likewise accepted-and-dropped, never forwarded); the row emits
        // combinationally on its w1 cycle as {w1, w0}, so bypass still adds
        // zero row latency. bypass_w0 needs no reset: phase discipline
        // guarantees a row's w0 is latched before its w1 cycle reads it, and
        // the output is only valid at phase 1.
        // 2*IN_WIDTH == OUT_WIDTH at every SLOTS, so the bypass gearbox holds
        // exactly the first half of an output beat
        logic [IN_WIDTH-1:0] bypass_w0;
        logic [OUT_WIDTH-1:0] emit_beat;
        always_comb begin
            for (int s = 0; s < SLOTS; s++) begin
                emit_beat[128*s +: 128] = emit_row[s];
            end
        end
        always_ff @(posedge clk) begin
            if (input_fire && (state == S_RUN) && eff_bypass && !bypass_phase) begin
                bypass_w0 <= input_data;
            end
        end
        assign input_ready = (state == S_DISCARD) ? 1'b1
            : (state != S_RUN)                    ? 1'b0
            : eff_bypass                          ? (!emit_busy && (output_ready || !bypass_phase))
                                                  : emit_free;
        assign output_valid = emit_busy || (bypass_pass && input_valid && bypass_phase);
        assign output_data = emit_busy ? emit_beat : {input_data, bypass_w0};
        assign output_last = emit_busy ? emit_last : (bypass_pass && input_last && bypass_phase);
    end else begin : g_out_narrow
        // the emit engine owns the output whenever it holds a beat; otherwise a
        // bypass batch drives the output straight from the input word (zero
        // added latency), suppressing only a torn w0. A torn bypass row is
        // input_last on a w0 (phase 0): it is never forwarded (dropped like the
        // other quad-row tiles) and never tagged last — the batch is
        // invalidated with ERR_STREAM instead.
        logic bypass_torn;
        assign bypass_torn = bypass_pass && input_last && !bypass_phase;
        assign input_ready = (state == S_DISCARD) ? 1'b1
            : (state != S_RUN)                    ? 1'b0
            : eff_bypass                          ? (!emit_busy && (output_ready || bypass_torn))
                                                  : emit_free;
        assign output_valid = emit_busy || (bypass_pass && input_valid && !bypass_torn);
        assign output_data = emit_busy ? ((emit_state == EMIT_W0) ? emit_row[0][63:0] : emit_row[0][127:64]) : input_data;
        assign output_last = emit_busy ? ((emit_state == EMIT_W1) && emit_last) : (bypass_pass && input_last && bypass_phase);
    end

    assign first_row = !batch_open;
    assign eff_bypass = first_row ? cfg_bypass : active_bypass;

    assign eff_offset0 = first_row ? cfg_field0_offset : active_offset0;
    assign eff_width0 = first_row ? cfg_field0_width : active_width0;
    assign eff_signed0 = first_row ? cfg_field0_signed : active_signed0;
    assign eff_offset1 = first_row ? cfg_field1_offset : active_offset1;
    assign eff_width1 = first_row ? cfg_field1_width : active_width1;
    assign eff_signed1 = first_row ? cfg_field1_signed : active_signed1;
    assign eff_offset2 = first_row ? cfg_field2_offset : active_offset2;
    assign eff_width2 = first_row ? cfg_field2_width : active_width2;
    assign eff_signed2 = first_row ? cfg_field2_signed : active_signed2;
    assign eff_offset3 = first_row ? cfg_field3_offset : active_offset3;
    assign eff_width3 = first_row ? cfg_field3_width : active_width3;
    assign eff_signed3 = first_row ? cfg_field3_signed : active_signed3;

    // config precedence: a descriptor with width 0, width > 32, or a field
    // running past bit 64 is invalid (checked at the first row and latched)
    function automatic logic field_cfg_ok(input logic [5:0] offset, input logic [5:0] width);
        begin
            field_cfg_ok = (width >= 6'd1) && (width <= 6'd32) && (({1'b0, offset} + {1'b0, width}) <= 7'd64);
        end
    endfunction

    assign config_ok = field_cfg_ok(cfg_field0_offset, cfg_field0_width)
        && field_cfg_ok(cfg_field1_offset, cfg_field1_width)
        && field_cfg_ok(cfg_field2_offset, cfg_field2_width)
        && field_cfg_ok(cfg_field3_offset, cfg_field3_width);

    // barrel-extract one field to LSB, mask to width, then sign/zero-extend to
    // int32 (the exact 32-bit pattern a downstream signed compare reads back)
    function automatic logic [31:0] extract_field(
        input logic [63:0] packed_row,
        input logic [5:0]  offset,
        input logic [5:0]  width,
        input logic        signed_ext
    );
        logic [63:0] shifted;
        logic [31:0] mask;
        logic        sign_bit;
        logic [31:0] raw;
        begin
            shifted = packed_row >> offset;
            // width low bits set (width in [1,32]; width==32 => all ones)
            mask = (width >= 6'd32) ? 32'hFFFF_FFFF : ((32'd1 << width) - 32'd1);
            raw = shifted[31:0] & mask;
            sign_bit = shifted[width - 6'd1];     // the field's top bit
            if (signed_ext && sign_bit) begin
                extract_field = raw | ~mask;
            end else begin
                extract_field = raw;
            end
        end
    endfunction

    // one widener per slot: w0 = {value, key}, w1 = {aux1, aux0}. A reserved
    // all-ones packed word is a ragged-tail pad and passes through as the
    // reserved PAD row instead of being sliced (SLOTS > 1 only — at SLOTS == 1
    // a beat is always exactly one row, so no pad can occur and the
    // silicon-proven 64/128 behaviour is untouched).
    for (genvar s = 0; s < SLOTS; s++) begin : g_slot
        wire [63:0] packed_s = input_data[64*s +: 64];
        wire        is_pad   = (SLOTS > 1) && (packed_s == PAD_PACKED);
        assign row_out[s] = is_pad ? PAD_ROW : {
            extract_field(packed_s, eff_offset3, eff_width3, eff_signed3),
            extract_field(packed_s, eff_offset2, eff_width2, eff_signed2),
            extract_field(packed_s, eff_offset1, eff_width1, eff_signed1),
            extract_field(packed_s, eff_offset0, eff_width0, eff_signed0)
        };
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            case (emit_state)
                EMIT_W0: begin
                    if (output_fire) begin
                        // WIDE: the single 128-bit beat was the whole row
                        emit_state <= WIDE ? EMIT_IDLE : EMIT_W1;
                    end
                end
                EMIT_W1: begin
                    if (output_fire) begin
                        emit_state <= EMIT_IDLE;
                    end
                end
                default: ;
            endcase

            case (state)
                S_RUN: begin
                    if (input_fire) begin
                        if (first_row) begin
                            batch_open <= 1'b1;
                            active_bypass <= cfg_bypass;
                            active_offset0 <= cfg_field0_offset;
                            active_width0 <= cfg_field0_width;
                            active_signed0 <= cfg_field0_signed;
                            active_offset1 <= cfg_field1_offset;
                            active_width1 <= cfg_field1_width;
                            active_signed1 <= cfg_field1_signed;
                            active_offset2 <= cfg_field2_offset;
                            active_width2 <= cfg_field2_width;
                            active_signed2 <= cfg_field2_signed;
                            active_offset3 <= cfg_field3_offset;
                            active_width3 <= cfg_field3_width;
                            active_signed3 <= cfg_field3_signed;
                        end
                        if (eff_bypass) begin
                            // BYPASS: the accepted 64-bit word is forwarded
                            // byte-identically on the output THIS cycle
                            // (input_fire == output_fire); nothing loads the
                            // emit engine. Two words per row, batch-last on the
                            // row's w1. Field descriptors are unused.
                            if (input_last && bypass_phase) begin
                                // last on a w1: well-framed batch, silent
                                // success (the downstream tile closes out)
                                bypass_phase <= 1'b0;
                                batch_open <= 1'b0;
                            end else if (input_last) begin
                                // torn row: last on a w0. The emit engine is
                                // idle in bypass (the accept gate requires it),
                                // so there is no in-flight beat to drain — raise
                                // the ERR_STREAM close-out directly.
                                raise_error(ERR_STREAM);
                            end else begin
                                bypass_phase <= ~bypass_phase;
                            end
                        end else if (first_row && !config_ok) begin
                            // config precedence over stream shape: drain the
                            // batch, then CONFIG (never emit a mis-sliced row)
                            if (input_last) begin
                                if (emit_state == EMIT_IDLE) begin
                                    raise_error(ERR_CONFIG);
                                end else begin
                                    batch_open <= 1'b0;
                                    state <= S_ERR_DRAIN;
                                end
                            end else begin
                                state <= S_DISCARD;
                            end
                        end else begin
                            // widen this packed row and emit its quad-row pair
                            start_emit(input_last);
                            if (input_last) begin
                                // silent success: the downstream tile closes out
                                batch_open <= 1'b0;
                            end
                        end
                    end
                end
                S_DISCARD: begin
                    if (input_fire && input_last) begin
                        if (emit_state == EMIT_IDLE) begin
                            raise_error(ERR_CONFIG);
                        end else begin
                            state <= S_ERR_DRAIN;
                        end
                    end
                end
                S_ERR_DRAIN: begin
                    // hold input closed until the in-flight output beat has
                    // drained, then raise the config-error close-out
                    if (emit_state == EMIT_IDLE) begin
                        raise_error(ERR_CONFIG);
                    end
                end
                S_STATUS: begin
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= S_RUN;
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    task automatic start_emit(input logic last_row);
        begin
            for (int s = 0; s < SLOTS; s++) begin
                emit_row[s] <= row_out[s];
            end
            emit_last <= last_row;
            emit_state <= EMIT_W0;
        end
    endtask

    task automatic raise_error(input logic [7:0] code);
        begin
            batch_open <= 1'b0;
            bypass_phase <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= S_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= S_RUN;
            emit_state <= EMIT_IDLE;
            batch_open <= 1'b0;
            active_bypass <= 1'b0;
            bypass_phase <= 1'b0;
            active_offset0 <= 6'd0;
            active_width0 <= 6'd0;
            active_signed0 <= 1'b0;
            active_offset1 <= 6'd0;
            active_width1 <= 6'd0;
            active_signed1 <= 1'b0;
            active_offset2 <= 6'd0;
            active_width2 <= 6'd0;
            active_signed2 <= 1'b0;
            active_offset3 <= 6'd0;
            active_width3 <= 6'd0;
            active_signed3 <= 1'b0;
            for (int s = 0; s < SLOTS; s++) begin
                emit_row[s] <= 128'd0;
            end
            emit_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask
endmodule

`default_nettype wire

`default_nettype none

// Key-set membership filter for 16-byte quad rows (two 64-bit words,
// w0 = {value, key}, w1 = {aux1, aux0}). The entry to the join tier: whole
// rows keep or drop by whether the row key (w0.lo, signed int32) is a member
// of a build set held in an on-chip bitmap. The build set is loaded in a LOAD
// phase BEFORE the PROBE stream (the two-phase membership job):
//
//   * cfg_load=1 (LOAD phase): the input stream is the KEY_SPACE-bit bitmap
//     image, KEY_SPACE/64 little-endian 64-bit words (word w bit b is id
//     64*w+b), written straight into the membership memory. No output rows;
//     the batch closes out with a success status.
//   * cfg_load=0 (PROBE phase): the input stream is probe rows; the filter
//     forwards the rows the mode selects and closes out with a success status.
//
// Membership is EXACT: a direct-mapped bitmap over the configured
// dictionary-id key space [0, KEY_SPACE) — no false positives and no false
// negatives. A probe key outside [0, KEY_SPACE) is a non-member (the build set
// only ever holds in-range dictionary ids; negatives and ids >= KEY_SPACE are
// caught by one unsigned range compare). cfg_mode selects semi (keep members:
// IN / semi-join / distinct probe) vs anti (keep non-members: NOT IN /
// anti-join). cfg_kind reserves the membership structure: v0 implements
// EXACT_BITMAP (1); BLOOM (2, a future hashed inexact bitmap) and the
// reserved/unconfigured values report ERR_CONFIG.
// dau_core.operators.filter_rows_by_membership is the probe golden;
// MembershipBuildSet.image_words is the load-image golden.
//
// Status contract (silent on success, so the probe filter is a legal mid-lane
// chain stage before an aggregation terminal — the lane status mux allows one
// close-out and the terminal owns it): a PROBE batch that forwards rows is
// silent (the downstream terminal's status is authoritative); a probe batch
// that forwards ZERO rows closes out with a success status (the compaction the
// downstream cannot see); a LOAD batch closes out with a success status (a
// load job has no downstream terminal — the close-out is the load-done
// signal). A torn probe row (input_last on a w0) reports ERR_STREAM; a bad
// kind config reports ERR_CONFIG (config precedence: the batch drains first);
// a build-set image whose length != KEY_SPACE/64 reports ERR_LOAD.
// selected_count reports the rows forwarded by the current / last probe batch
// (0 after a load or an error close-out). Row atomicity: input_last is legal
// only on w1 of a probe row; the newest passing row is held in a skid slot so
// input_last lands on the final forwarded word even when trailing rows drop.
//
// Timing (the membership-mux retime): the bitmap read is TWO register stages
// — the per-primitive BRAM output registers, then a fabric register behind
// the multi-BRAM output mux — so the ~170-BRAM mux of a KEY_SPACE=6M bitmap
// gets its own cycle (the 125 MHz critical path before the retime). The
// keep/drop decision therefore lands one cycle after a row's w1: the
// completed row waits in a pending slot and the decision applies during the
// next row's w0 cycle, so the steady-state probe rate stays one row per two
// cycles.
module dau_int32_key_membership_filter #(
    // dictionary-id space (bitmap width in bits); a positive multiple of 64.
    // Sized to the build dictionary's cardinality — the memory stays
    // BRAM-resident and the image is KEY_SPACE/64 words.
    parameter int KEY_SPACE = 4096
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        cfg_load,   // 1 = load the bitmap image, 0 = probe rows
    input  wire logic        cfg_mode,   // 0 = semi (keep members), 1 = anti (keep non-members)
    input  wire logic [1:0]  cfg_kind,   // membership structure: 1 = EXACT_BITMAP (v0)
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
    output logic [63:0]      selected_count
);
    localparam int WORDS = KEY_SPACE / 64;
    localparam int AWIDTH = (WORDS <= 1) ? 1 : $clog2(WORDS);
    localparam logic [AWIDTH-1:0] LAST_WORD = AWIDTH'(WORDS - 1);
    localparam logic [31:0] KEY_SPACE_U = KEY_SPACE;

    localparam logic [1:0] KIND_EXACT_BITMAP = 2'd1;
    localparam logic       MODE_ANTI = 1'b1;

    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;
    localparam logic [7:0] ERR_LOAD   = 8'd3;

    typedef enum logic [3:0] {
        S_ARM,        // batch idle: the first word decides load vs probe (cfg latches)
        S_LOAD,       // writing bitmap image words
        S_W0,         // probe: accept a row's w0, issue the membership read
        S_W1,         // probe: accept w1 into the pending slot (decision lands next cycle)
        S_DECIDE_LAST,// probe: final row pending — wait for its decision before the tail
        S_FLUSH,      // probe: release the held final row with last=1 (silent on success)
        S_FLUSH_DRAIN,// hold input closed until the flushed final beat drains, then re-arm
        S_ERR_DRAIN,  // torn row: drain the in-flight forwarded beat, then ERR_STREAM
        S_DISCARD,    // drain a bad-kind batch to input_last, then ERR_CONFIG
        S_DRAIN_LOAD, // drain an over-long image to input_last, then ERR_LOAD
        S_STATUS      // hold a close-out (success or error) until accepted
    } state_t;

    typedef enum logic [1:0] {
        EMIT_IDLE,
        EMIT_W0,
        EMIT_W1
    } emit_t;

    state_t state;
    emit_t  emit_state;

    // the direct-mapped membership bitmap: WORDS x 64 bits, one write port
    // (LOAD) and one registered read port (PROBE), never both in a cycle.
    // ram_style=block keeps it BRAM-resident so it scales to large dictionaries
    // without burning LUTs (the join tier's binding constraint is BRAM).
    (* ram_style = "block" *) logic [63:0] membership_mem [0:WORDS-1];
    logic [63:0] read_word;       // stage 1: the BRAM primitives' output registers
    logic [5:0]  probe_bit;
    logic        probe_in_range;
    logic [63:0] read_word2;      // stage 2: behind the multi-BRAM output mux
    logic [5:0]  probe_bit2;
    logic        probe_in_range2;
    logic        addr_issued;     // the row_w0-derived address drove the read last cycle
    logic        stage1_valid;    // a read landed in stage 1 last cycle
    logic        result_ready;    // stage 2 holds the current row's unconsumed result

    // the completed row awaiting its (stage-2) keep/drop decision
    logic        pend_valid;
    logic [63:0] pend_w0;
    logic [63:0] pend_w1;
    logic        pend_resolved;   // decision latched (survives stage-2 reuse)
    logic        pend_keep_r;

    logic [AWIDTH-1:0] load_counter;

    // batch-latched config (sampled at S_ARM so a mid-batch cfg change cannot
    // tear the phase / mode)
    logic active_mode;

    logic [63:0] row_w0;
    logic        held_valid;
    logic [63:0] held_w0;
    logic [63:0] held_w1;
    logic [63:0] emit_w0;
    logic [63:0] emit_w1;
    logic        emit_last;

    logic input_fire;
    logic output_fire;
    logic emit_free;

    logic [31:0] probe_key;
    logic        probe_key_in_range;
    logic [AWIDTH-1:0] probe_raddr;
    logic        probe_w0_accept;
    logic        mem_we;
    logic [AWIDTH-1:0] mem_waddr;

    logic is_member;
    logic row_keep;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign emit_free = (emit_state == EMIT_IDLE) || ((emit_state == EMIT_W1) && output_fire);

    always_comb begin
        unique case (state)
            S_W1:    input_ready = (!pend_valid || pend_can_apply);
            S_DECIDE_LAST, S_FLUSH, S_FLUSH_DRAIN, S_ERR_DRAIN, S_STATUS: input_ready = 1'b0;
            default: input_ready = 1'b1;  // S_ARM, S_LOAD, S_W0, S_DISCARD, S_DRAIN_LOAD
        endcase
    end

    assign output_valid = emit_state != EMIT_IDLE;
    assign output_data = (emit_state == EMIT_W0) ? emit_w0 : emit_w1;
    assign output_last = (emit_state == EMIT_W1) && emit_last;

    // membership read address: the row key (w0.lo) from the REGISTERED
    // row_w0 — sourcing the 192-BRAM address fan-out from a register (which
    // the tools replicate across the die) instead of the upstream reader's
    // combinational data path was the composed-build critical path. The read
    // therefore lands one cycle later (three stages total); the pending-row
    // decision machinery absorbs it just-in-time, so the steady-state probe
    // rate stays one row per two cycles. A key outside [0, KEY_SPACE) fails
    // the unsigned range compare (negatives read as >= 2**31 > KEY_SPACE_U)
    // and is forced to a non-member; the address is clamped so an
    // out-of-range key never indexes past the memory.
    assign probe_key = row_w0[31:0];
    assign probe_key_in_range = (probe_key < KEY_SPACE_U);
    assign probe_raddr = probe_key_in_range ? probe_key[6 +: AWIDTH] : '0;

    // a probe w0 is being accepted this cycle (S_ARM's first word in probe
    // phase, or a subsequent S_W0); the read is issued here and lands in S_W1
    assign probe_w0_accept = input_fire
        && (((state == S_ARM) && !cfg_load && (cfg_kind == KIND_EXACT_BITMAP) && !input_last)
            || ((state == S_W0) && !input_last));

    // image writes: S_ARM's word 0 in load phase, then each S_LOAD word
    always_comb begin
        mem_we = 1'b0;
        mem_waddr = '0;
        if (input_fire) begin
            if ((state == S_ARM) && cfg_load && (cfg_kind == KIND_EXACT_BITMAP)) begin
                mem_we = 1'b1;
                mem_waddr = '0;
            end else if (state == S_LOAD) begin
                mem_we = 1'b1;
                mem_waddr = load_counter;
            end
        end
    end

    // the keep/drop decision reads STAGE 2 (behind the retimed mux)
    assign is_member = probe_in_range2 && read_word2[probe_bit2];
    assign row_keep = (active_mode == MODE_ANTI) ? !is_member : is_member;

    // pending-row decision: ready once resolved (latched) or stage 2 holds
    // this row's result; a drop applies unconditionally, a keep needs a slot.
    // A torn w0 firing this cycle aborts the batch — suppress the apply so
    // the pending row drops with the batch (old-contract parity). input_valid
    // (not input_fire) keeps input_ready loop-free: S_W0 is unconditionally
    // ready, so the two are identical there.
    logic torn_w0_fire;
    logic pend_decision_ready;
    logic pend_keep_now;
    logic pend_can_apply;
    assign torn_w0_fire = (state == S_W0) && input_valid && input_last;
    assign pend_decision_ready = pend_valid && (pend_resolved || result_ready);
    assign pend_keep_now = pend_resolved ? pend_keep_r : row_keep;
    assign pend_can_apply = pend_decision_ready && !torn_w0_fire && (!pend_keep_now || !held_valid || emit_free);

    // zero-init so a probe before any load is a well-defined empty build set
    // (Vivado carries the INIT into the BRAM)
    initial begin
        for (int i = 0; i < WORDS; i++) membership_mem[i] = 64'd0;
        read_word = 64'd0;
        probe_bit = 6'd0;
        probe_in_range = 1'b0;
        read_word2 = 64'd0;
        probe_bit2 = 6'd0;
        probe_in_range2 = 1'b0;
    end

    always_ff @(posedge clk) begin
        if (mem_we) begin
            membership_mem[mem_waddr] <= input_data;
        end
        // the read port is ALWAYS-ON: gating it on probe_w0_accept put the
        // whole input_ready comb chain onto the 192-BRAM enable fan-out.
        // The address derives from row_w0, so the read for a row issues the
        // cycle after its w0 accept; between real reads read_word churns,
        // but stage 2 only captures when stage1_valid marks a probe read.
        read_word <= membership_mem[probe_raddr];
        if (addr_issued) begin
            probe_bit <= probe_key[5:0];
            probe_in_range <= probe_key_in_range;
        end
        if (stage1_valid) begin
            // the fabric register behind the multi-BRAM output mux (stage 2)
            read_word2 <= read_word;
            probe_bit2 <= probe_bit;
            probe_in_range2 <= probe_in_range;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            case (emit_state)
                EMIT_W0: if (output_fire) emit_state <= EMIT_W1;
                EMIT_W1: if (output_fire) emit_state <= EMIT_IDLE;
                default: ;
            endcase

            // read-pipeline bookkeeping + the pending-row decision engine
            // (runs before the state case so batch-end paths can override)
            addr_issued <= probe_w0_accept;
            stage1_valid <= addr_issued;
            if (stage1_valid) begin
                result_ready <= 1'b1;
            end
            if (pend_decision_ready && !pend_resolved) begin
                pend_keep_r <= pend_keep_now;
                pend_resolved <= 1'b1;
                result_ready <= 1'b0;  // stage 2 consumed for this row
            end
            if (pend_can_apply) begin
                pend_valid <= 1'b0;
                pend_resolved <= 1'b0;
                if (pend_keep_now) begin
                    if (held_valid) start_emit(held_w0, held_w1, 1'b0);
                    held_valid <= 1'b1;
                    held_w0 <= pend_w0;
                    held_w1 <= pend_w1;
                    selected_count <= selected_count + 64'd1;
                end
            end

            case (state)
                S_ARM: begin
                    if (input_fire) begin
                        active_mode <= cfg_mode;
                        selected_count <= 64'd0;
                        if (cfg_kind != KIND_EXACT_BITMAP) begin
                            // config precedence over stream shape: unconfigured /
                            // unsupported kind (bloom is not built in v0)
                            if (input_last) begin
                                raise_error(ERR_CONFIG);
                            end else begin
                                held_valid <= 1'b0;
                                state <= S_DISCARD;
                            end
                        end else if (cfg_load) begin
                            // LOAD phase: input_data is image word 0 (written by mem_we)
                            if (WORDS == 1) begin
                                if (input_last) raise_success();
                                else state <= S_DRAIN_LOAD;  // over-long image
                            end else if (input_last) begin
                                raise_error(ERR_LOAD);       // short image (one word)
                            end else begin
                                load_counter <= AWIDTH'(1);
                                state <= S_LOAD;
                            end
                        end else begin
                            // PROBE phase: input_data is row w0 (read issued above)
                            if (input_last) begin
                                raise_error(ERR_STREAM);     // torn row
                            end else begin
                                row_w0 <= input_data;
                                state <= S_W1;
                            end
                        end
                    end
                end
                S_LOAD: begin
                    if (input_fire) begin
                        if (load_counter == LAST_WORD) begin
                            if (input_last) raise_success();
                            else state <= S_DRAIN_LOAD;      // over-long image
                        end else if (input_last) begin
                            raise_error(ERR_LOAD);           // short image
                        end else begin
                            load_counter <= load_counter + AWIDTH'(1);
                        end
                    end
                end
                S_W0: begin
                    if (input_fire) begin
                        if (input_last) begin
                            // torn row: the batch is invalid (the pending row
                            // drops with it — its apply was suppressed this
                            // cycle). If a forwarded beat is still in flight,
                            // drain it before the error close-out so status
                            // cannot overtake it.
                            pend_valid <= 1'b0;
                            pend_resolved <= 1'b0;
                            result_ready <= 1'b0;
                            if (emit_state == EMIT_IDLE) begin
                                raise_error(ERR_STREAM);
                            end else begin
                                held_valid <= 1'b0;
                                selected_count <= 64'd0;
                                state <= S_ERR_DRAIN;
                            end
                        end else begin
                            row_w0 <= input_data;
                            state <= S_W1;
                        end
                    end
                end
                S_W1: begin
                    if (input_fire) begin
                        // the completed row parks in the pending slot; its
                        // keep/drop lands next cycle from the retimed stage 2
                        pend_valid <= 1'b1;
                        pend_w0 <= row_w0;
                        pend_w1 <= input_data;
                        pend_resolved <= 1'b0;
                        state <= input_last ? S_DECIDE_LAST : S_W0;
                    end
                end
                S_DECIDE_LAST: begin
                    // the engine applies the final pending row; then the tail
                    if (!pend_valid) begin
                        if (held_valid) begin
                            state <= S_FLUSH;
                        end else begin
                            raise_success();                 // zero forwarded rows: the compaction close-out
                        end
                    end
                end
                S_FLUSH: begin
                    // release the held final row tagged last; do NOT re-arm yet
                    // (input stays closed via S_FLUSH_DRAIN) so a following
                    // batch's close-out cannot overtake this still-in-flight tail
                    if (emit_free) begin
                        start_emit(held_w0, held_w1, 1'b1);
                        held_valid <= 1'b0;
                        state <= S_FLUSH_DRAIN;
                    end
                end
                S_FLUSH_DRAIN: begin
                    // re-arm only once the flushed final beat has drained from
                    // the emit engine (silent on success — the downstream
                    // terminal owns the success status)
                    if (emit_state == EMIT_IDLE) begin
                        state <= S_ARM;
                    end
                end
                S_ERR_DRAIN: begin
                    // hold input closed until the in-flight forwarded beat has
                    // drained, then raise the torn-row error close-out
                    if (emit_state == EMIT_IDLE) begin
                        raise_error(ERR_STREAM);
                    end
                end
                S_DISCARD: begin
                    if (input_fire && input_last) raise_error(ERR_CONFIG);
                end
                S_DRAIN_LOAD: begin
                    if (input_fire && input_last) raise_error(ERR_LOAD);
                end
                S_STATUS: begin
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= S_ARM;
                    end
                end
                default: reset_state();
            endcase
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

    task automatic raise_success;
        begin
            status_valid <= 1'b1;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            state <= S_STATUS;
        end
    endtask

    // error close-out: drop the held row and zero the count. The emit engine is
    // left to drain any in-flight beat normally (never drop a valid-but-unfired
    // output beat); the batch is invalid, so the downstream aborts it on the
    // error status.
    task automatic raise_error(input logic [7:0] code);
        begin
            held_valid <= 1'b0;
            pend_valid <= 1'b0;
            pend_resolved <= 1'b0;
            result_ready <= 1'b0;
            selected_count <= 64'd0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= S_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= S_ARM;
            emit_state <= EMIT_IDLE;
            active_mode <= 1'b0;
            load_counter <= '0;
            row_w0 <= 64'd0;
            addr_issued <= 1'b0;
            stage1_valid <= 1'b0;
            result_ready <= 1'b0;
            pend_valid <= 1'b0;
            pend_w0 <= 64'd0;
            pend_w1 <= 64'd0;
            pend_resolved <= 1'b0;
            pend_keep_r <= 1'b0;
            held_valid <= 1'b0;
            held_w0 <= 64'd0;
            held_w1 <= 64'd0;
            emit_w0 <= 64'd0;
            emit_w1 <= 64'd0;
            emit_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            selected_count <= 64'd0;
        end
    endtask
endmodule

`default_nettype wire

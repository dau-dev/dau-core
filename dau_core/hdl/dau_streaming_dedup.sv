`default_nettype none

`include "types.svh"

// Streaming insert-on-probe dedup for 16-byte quad rows (two 64-bit words,
// w0 = {value, key}, w1 = {aux1, aux0}): COUNT(DISTINCT key) on the device.
// The row key (w0.lo, the row's first KEY_WIDTH lane) indexes a direct-mapped
// bitmap over the dictionary-id space [0, KEY_SPACE); a row whose bit is CLEAR sets it and
// increments the distinct count, a row whose bit is already set changes
// nothing. Rows are not forwarded — at input_last the tile emits ONE 64-bit
// word, the distinct count, with output_last set, then a success close-out
// status (the terminal-tile convention: the record writer's done waits on a
// consumed producer status). N keys in, one count out is the whole point:
// this is the rare operator whose output does not cross the bus.
// dau_core.operators.count_distinct_rows is the golden.
//
// The membership filter is this tile's static sibling — the same
// direct-mapped bitmap, loaded once in a build phase and then probed
// READ-ONLY. Dedup writes the bitmap while it probes, and that
// read-modify-write is the entire difference between the two tiles. It is
// also the only thing here that is hard, for the reason below.
//
// READ-DURING-WRITE, and why FORWARDING rather than a stall. A row's bitmap
// word is read when its w0 is accepted and written back one cycle after its
// w1, which is the same cycle the NEXT row's w0 issues its read (the steady
// state is one row per two cycles). Two rows carrying the same key — or any
// two keys in the same 64-bit word, so any two ids within 64 of each other —
// therefore collide: the second row's read is issued in the cycle the first
// row's write happens, and a BRAM read concurrent with a write to the same
// address returns the OLD word. Left alone that double-counts a repeated key
// and, worse, silently drops the first row's bit when the second row writes
// its own stale word back. The fix is a one-deep forward: when a read is
// issued in the same cycle as a write to the same address, the value written
// is muxed into the read path instead of the memory output. One cycle of
// forwarding is exactly enough — a write is visible to any read issued in a
// LATER cycle, and no read can be issued earlier than the colliding one,
// because a row's w0 cannot precede the previous row's w1. A stall was the
// alternative and is the wrong trade here: duplicate keys are not the
// exceptional case in a dedup, they are the workload, so stalling on them
// would give up the ingest rate exactly where the tile is meant to earn it.
// The forward mux sits on the stage-1 -> stage-2 hop, which keeps the memory
// read itself pure (a clean BRAM inference) and puts the mux in the fabric
// stage that the membership retime already showed has the slack.
//
// The bitmap SURVIVES a batch boundary, and must. A scan split into
// overlapped batches is one dedup: per-batch counts cannot be summed, since
// a key appearing in two batches would be counted twice. So an accumulation
// runs across batches and the final batch's count is the answer. cfg_clear
// starts a fresh one: it is sampled WHEN A BATCH BEGINS — the cycle the first
// word is offered — and sweeps the bitmap to zero and zeroes the count before
// admitting it (the sweep costs KEY_SPACE/64 cycles, 0.2% of a million-row
// batch at the largest bitmap that fits). Sampling at the batch's first word
// rather than whenever the tile is idle is what makes a level-held cfg_clear
// safe: a config register left high in the gap between batches requests
// per-batch dedup, and cannot wipe a live accumulation before its next batch
// arrives. Hold it high for per-batch dedup; raise it only on the first batch
// of a scan to dedup the whole scan. The tile also sweeps out of reset, so a
// power-on bitmap is empty without relying on the memory's INIT surviving to
// hardware.
//
// Status contract (success_close_out=True — a terminal tile): every batch
// closes out, with the count record first and the status after it drains.
// A key outside [0, KEY_SPACE) is ERR_RANGE: unlike the membership filter,
// where an unrepresentable key is honestly a non-member, a dedup has nowhere
// to record it and wrapping it would merge two distinct keys into one count,
// so the batch drains to input_last and closes out in error. A torn row
// (input_last on a w0) is ERR_STREAM. An error close-out zeroes
// distinct_count AND re-sweeps the bitmap: rows before the bad one already
// set their bits, and a half-inserted bitmap is not a state an accumulation
// can resume from — the host restarts it. (The shell reaches the same
// conclusion from the other side: a job error asserts the lane reset.)
module dau_streaming_dedup #(
    // dictionary-id space (bitmap width in bits); a multiple of 64, sized to
    // the column's CARDINALITY rather than its row count — one bit per
    // distinct key is what makes this fit on-chip where a join build side
    // does not. The maximum is the largest bitmap that fits the BRAM budget
    // of the boards this palette targets (8 Mbit = 228 RAMB36).
    parameter int KEY_SPACE = 4096,
    // The ELEMENT TYPE of the key lane, as encoded by types.svh. A plain
    // signed integer encodes as its own width, so `DAU_TYPE_INT32 is 32 and
    // this reads exactly the w0.lo key the tile has always read. The key is
    // a DICTIONARY ID — an index, not a magnitude — so the range test is one
    // unsigned compare against KEY_SPACE at either signedness, and it is a
    // float that has no meaning here rather than an unsigned integer.
    // A narrower type does not shrink the bitmap: KEY_SPACE does that, and it
    // is the cardinality that sizes the memory.
    parameter int KEY_WIDTH = `DAU_TYPE_INT32
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        cfg_clear,  // 1 = sweep the bitmap and zero the count before this batch
    input  wire logic        input_valid,
    output logic             input_ready,
    // only w0.lo — the row key — is read. A dedup emits no rows, so the value
    // and aux fields are consumed and discarded rather than carried; the row
    // still costs two beats because the lane's record IS a quad row.
    /* verilator lint_off UNUSEDSIGNAL */
    input  wire logic [63:0] input_data,
    /* verilator lint_on UNUSEDSIGNAL */
    input  wire logic        input_last,
    output logic             output_valid,
    input  wire logic        output_ready,
    output logic [63:0]      output_data,
    output logic             output_last,
    output logic             status_valid,
    input  wire logic        status_ready,
    output logic             status_error,
    output logic [7:0]       status_error_code,
    output logic [63:0]      distinct_count
);
    localparam int WORDS = KEY_SPACE / 64;
    localparam int AWIDTH = (WORDS <= 1) ? 1 : $clog2(WORDS);
    localparam logic [AWIDTH-1:0] LAST_WORD = AWIDTH'(WORDS - 1);
    localparam logic [31:0] KEY_SPACE_U = KEY_SPACE;
    localparam int KEY_BITS     = `DAU_TYPE_WIDTH(KEY_WIDTH);
    localparam int KEY_IS_FLOAT = `DAU_TYPE_IS_FLOAT(KEY_WIDTH);

    // no ERR_CONFIG: cfg_clear is one bit with no invalid encoding, so this
    // tile has no configuration it can reject. The codes it does raise keep
    // the palette's values (2 = torn row everywhere; 3 = the range reject the
    // broadcast hash join gives the same condition).
    localparam logic [7:0] ERR_STREAM = 8'd2;
    localparam logic [7:0] ERR_RANGE = 8'd3;

    if (KEY_SPACE < 64 || KEY_SPACE > 8388608) begin : g_key_space_range
        $error("KEY_SPACE must be in [64, 8388608]");
    end
    if (KEY_SPACE % 64 != 0) begin : g_key_space_grid
        $error("KEY_SPACE must be a multiple of 64");
    end
    // the key is one lane of the {value, key} word, so a pair of lanes tiles
    // it; the id space itself tops out at 2**23, so a wider key would address
    // nothing the bitmap can hold
    if (KEY_BITS < 8 || KEY_BITS > 32 || (64 % KEY_BITS) != 0) begin : g_key_width_check
        $error("KEY_WIDTH must divide 64 and lie in [8, 32]");
    end
    // A TYPE IS NOT A CAPABILITY. A float bit pattern is not an index into
    // anything: its exponent bits would select a bitmap word and its mantissa
    // a bit within it, which counts distinct BIT PATTERNS while claiming to
    // count distinct values. Refuse it at elaboration.
    if (KEY_IS_FLOAT != 0) begin : g_key_float_check
        $error("KEY_WIDTH must not name a floating-point type: the key is a dictionary id, an integer index");
    end

    typedef enum logic [2:0] {
        S_CLEAR,    // sweep the bitmap to zero (from reset, from cfg_clear, and after an error close-out)
        S_ARM,      // batch idle: a requested sweep runs here, then the first w0 is accepted
        S_W0,       // accept a row's w0, range-check the key, issue the bitmap read
        S_W1,       // accept a row's w1; the insert decision lands the next cycle
        S_SETTLE,   // final row of the batch: wait for its decision before reporting the count
        S_EMIT,     // the count word is in flight; close out once it drains
        S_DISCARD,  // out-of-range key: drain the batch to input_last, then ERR_RANGE
        S_STATUS    // hold a close-out (success or error) until accepted
    } state_t;

    state_t state;

    // the direct-mapped dedup bitmap: WORDS x 64 bits, one write port (the
    // sweep and the insert share it) and one registered read port (the probe).
    // ram_style=block keeps it BRAM-resident, which is the point of the tile:
    // LUT-light and BRAM-heavy, sized by cardinality.
    (* ram_style = "block" *) logic [63:0] dedup_mem [0:WORDS-1];

    // read pipeline. Two register stages, as in the membership filter: the
    // BRAM primitives' own output registers, then a fabric register behind
    // the multi-BRAM output mux, so a large bitmap's mux gets its own cycle.
    // The forward mux rides the second hop.
    logic [63:0]       read_word;
    logic [5:0]        probe_bit;
    logic [AWIDTH-1:0] probe_addr;
    logic              fwd_pending;  // the read issued into stage 1 collided with the write of that cycle
    logic [63:0]       fwd_word;     // ... and this is what that write put there
    logic [63:0]       read_word2;
    logic [5:0]        probe_bit2;
    logic [AWIDTH-1:0] probe_addr2;
    logic              stage1_valid;
    logic              resolve_pending;  // stage 2 carries a row whose decision is due this cycle

    logic [AWIDTH-1:0] clear_addr;
    logic              clear_done;  // the bitmap is empty: a cfg_clear batch needs no further sweep

    logic [7:0] pending_err;

    logic input_fire;
    logic output_fire;

    logic [KEY_BITS-1:0] probe_key;
    logic [31:0]       probe_key_u;
    logic              key_in_range;
    logic [AWIDTH-1:0] probe_raddr;
    logic              probe_w0_accept;
    logic              clear_needed;

    logic              set_new;   // the resolved row's key was unseen: count it and set its bit
    logic [63:0]       set_word;  // its bitmap word with the bit set
    logic              mem_we;
    logic [AWIDTH-1:0] mem_waddr;
    logic [63:0]       mem_wdata;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;

    // the bitmap index: the row key (w0.lo). One unsigned compare catches
    // negatives (they read as >= 2**(KEY_BITS-1) > KEY_SPACE_U) and ids past
    // the space; the address is clamped so a rejected key never indexes past
    // the memory. The compare and the address slice run on the key
    // ZERO-EXTENDED to 32 bits: KEY_SPACE reaches 2**23 and the word address
    // is bits [6 +: AWIDTH] of the id, neither of which a narrow key could
    // hold, and at the default int32 the extension is the identity.
    assign probe_key = input_data[KEY_BITS-1:0];
    assign probe_key_u = 32'(probe_key);
    assign key_in_range = (probe_key_u < KEY_SPACE_U);
    assign probe_raddr = key_in_range ? probe_key_u[6+:AWIDTH] : '0;

    // a probe w0 is accepted this cycle (the batch's first word at S_ARM, or a
    // subsequent S_W0): the read is issued here and lands in stage 1 next cycle
    assign probe_w0_accept = input_fire && !input_last && key_in_range && ((state == S_ARM) || (state == S_W0));

    assign clear_needed = cfg_clear && !clear_done;

    // the insert decision, from STAGE 2 (behind the retimed mux)
    assign set_new = resolve_pending && !read_word2[probe_bit2];
    assign set_word = read_word2 | (64'd1 << probe_bit2);

    always_comb begin
        if (state == S_CLEAR) begin
            mem_we = 1'b1;
            mem_waddr = clear_addr;
            mem_wdata = 64'd0;
        end else begin
            mem_we = set_new;
            mem_waddr = probe_addr2;
            mem_wdata = set_word;
        end
    end

    always_comb begin
        unique case (state)
            S_ARM: input_ready = !clear_needed;
            S_W0, S_W1, S_DISCARD: input_ready = 1'b1;
            default: input_ready = 1'b0;  // S_CLEAR, S_SETTLE, S_EMIT, S_STATUS
        endcase
    end

    always_ff @(posedge clk) begin
        if (mem_we) begin
            dedup_mem[mem_waddr] <= mem_wdata;
        end
        if (probe_w0_accept) begin
            // the memory read stays pure; the collision is recorded beside it
            // and applied one hop later (see the READ-DURING-WRITE note above)
            read_word <= dedup_mem[probe_raddr];
            probe_bit <= probe_key_u[5:0];
            probe_addr <= probe_raddr;
            fwd_pending <= set_new && (probe_raddr == probe_addr2);
            fwd_word <= set_word;
        end
        if (stage1_valid) begin
            read_word2 <= fwd_pending ? fwd_word : read_word;
            probe_bit2 <= probe_bit;
            probe_addr2 <= probe_addr;
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
                output_last <= 1'b0;
            end

            // read-pipeline bookkeeping and the insert itself run BEFORE the
            // state case, so a batch-end path can override the count
            stage1_valid <= probe_w0_accept;
            resolve_pending <= (state == S_W1) && input_fire;
            if (set_new) begin
                distinct_count <= distinct_count + 64'd1;
            end

            case (state)
                S_CLEAR: begin
                    // sweep addr 0..LAST_WORD writing zero (mem_we above)
                    if (clear_addr == LAST_WORD) begin
                        clear_addr <= '0;
                        clear_done <= 1'b1;
                        distinct_count <= 64'd0;
                        state <= S_ARM;
                    end else begin
                        clear_addr <= clear_addr + AWIDTH'(1);
                    end
                end
                S_ARM: begin
                    if (input_valid && clear_needed) begin
                        // this batch asked for a fresh accumulation: sweep
                        // before admitting its first word (input_ready is
                        // already low), never in the idle gap before it
                        clear_addr <= '0;
                        state <= S_CLEAR;
                    end else if (input_fire) begin
                        if (input_last) begin
                            raise_error(ERR_STREAM);  // torn row
                        end else if (!key_in_range) begin
                            pending_err <= ERR_RANGE;
                            state <= S_DISCARD;  // drain this row's w1 and the rest
                        end else begin
                            state <= S_W1;  // read issued via probe_w0_accept
                        end
                    end
                end
                S_W0: begin
                    if (input_fire) begin
                        if (input_last) begin
                            raise_error(ERR_STREAM);  // torn row
                        end else if (!key_in_range) begin
                            pending_err <= ERR_RANGE;
                            state <= S_DISCARD;
                        end else begin
                            state <= S_W1;
                        end
                    end
                end
                S_W1: begin
                    if (input_fire) begin
                        // w1 carries no key: the row's decision is already in
                        // flight and lands next cycle (resolve_pending above)
                        state <= input_last ? S_SETTLE : S_W0;
                    end
                end
                S_SETTLE: begin
                    // the final row's insert resolves one cycle after its w1;
                    // reporting the count before that would miss it
                    if (!resolve_pending) begin
                        output_valid <= 1'b1;
                        output_data <= distinct_count;
                        output_last <= 1'b1;
                        state <= S_EMIT;
                    end
                end
                S_EMIT: begin
                    // success close-out only once the count word is accepted
                    // (the record writer's done waits on a consumed status)
                    if (!output_valid || output_ready) begin
                        status_valid <= 1'b1;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= S_STATUS;
                    end
                end
                S_DISCARD: begin
                    if (input_fire && input_last) begin
                        raise_error(pending_err);
                    end
                end
                S_STATUS: begin
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        // the next batch's cfg_clear decides afresh whether the
                        // accumulation continues or restarts
                        clear_done <= 1'b0;
                        if (status_error) begin
                            // the failed batch left its earlier rows' bits set:
                            // restart empty rather than resume from a partial
                            clear_addr <= '0;
                            state <= S_CLEAR;
                        end else begin
                            state <= S_ARM;
                        end
                    end
                end
                default: reset_state();
            endcase
        end
    end

    // error close-out: the batch is invalid, so the count reads zero and the
    // in-flight decision is abandoned. The bitmap sweep happens on the way out
    // of S_STATUS, once the status has been consumed.
    task automatic raise_error(input logic [7:0] code);
        begin
            stage1_valid <= 1'b0;
            resolve_pending <= 1'b0;
            distinct_count <= 64'd0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= S_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            // out of reset the bitmap is swept rather than assumed empty
            state <= S_CLEAR;
            clear_addr <= '0;
            clear_done <= 1'b0;
            // the read-pipeline DATA registers are deliberately absent: they
            // live in the memory process (one driver each, as the palette's
            // other BRAM tiles do) and are only ever read while stage1_valid /
            // resolve_pending say they carry a row, both of which reset here
            stage1_valid <= 1'b0;
            resolve_pending <= 1'b0;
            pending_err <= 8'd0;
            output_valid <= 1'b0;
            output_data <= 64'd0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            distinct_count <= 64'd0;
        end
    endtask
endmodule

`default_nettype wire

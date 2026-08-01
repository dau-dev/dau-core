`default_nettype none

// Broadcast hash join for 16-byte quad rows (two 64-bit words,
// w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th 32-bit lane in
// (key, value, aux0, aux1) order). The join tier's payload-emitting Joiner:
// a build side is broadcast into an on-chip direct-mapped payload table, then
// probe rows inner-join against it. A build/probe key is w0.lo (signed int32);
// the three data fields are (value, aux0, aux1). Symmetric to the membership
// bitmap, but each table slot stores {valid, payload} where payload is the
// build row's three data fields (b0, b1, b2). The job runs in two phases,
// selected by cfg_load (the two-phase join job):
//
//   * cfg_load=1 (LOAD phase): the input stream is build rows (key, b0, b1,
//     b2). The valid bits are swept clear first, then table[key] is written
//     {valid=1, b0, b1, b2}. A duplicate build key is last-writer-wins (the
//     build side is expected unique/PK). No output rows; the batch closes out
//     with a success status (the load-done signal).
//   * cfg_load=0 (PROBE phase): the input stream is probe rows (key, v0, v1,
//     v2). On table[key].valid the tile emits ONE joined quad row (inner
//     join); a miss drops. The emitted row is (key, out0, out1, out2) where
//     the key is the matched key and each out_i is chosen by cfg_out{i}_sel
//     from the six sources {b0, b1, b2, v0, v1, v2} (0..5), so a composition
//     can carry the build+probe columns it needs downstream (q3's
//     orders JOIN lineitem carrying orderdate/shippriority + revenue inputs).
//
// The table is a direct-mapped payload table over a bounded key space
// [0, KEY_SPACE) (a broadcast build side sized to fit BRAM). A build or probe
// key outside [0, KEY_SPACE) is rejected explicitly with ERR_RANGE (one
// unsigned range compare catches negatives and ids >= KEY_SPACE). The valid
// bit lives in the BRAM word next to the payload, so one read returns both;
// the LOAD phase sweeps every slot's valid bit to 0 before writing build rows,
// so a fresh load replaces the previous table. ram_style=block keeps the table
// BRAM-resident (the join tier's binding constraint is BRAM), zero-initialised
// so a probe before any load is a well-defined empty table.
// dau_core.operators.broadcast_hash_join_rows is the golden.
//
// Status contract (success_close_out=False — a mid-lane join stage before a
// downstream terminal that owns the batch close-out): a PROBE batch that emits
// rows is silent (the downstream terminal's status is authoritative); a PROBE
// batch that emits ZERO rows closes out with a success status (the compaction
// the downstream cannot see); a LOAD batch closes out with a success status
// (the load-done signal). A torn row (input_last on a w0) reports ERR_STREAM;
// an out-of-range key reports ERR_RANGE; a reserved/out-of-range output
// field-select reports ERR_CONFIG (config precedence: checked at the first
// probe row, the batch drains first). Exactly one close-out per batch. The
// newest joined row is held in a skid slot so input_last lands on the final
// emitted word even when trailing probe rows miss; the tile NEVER re-arms for
// a following batch or raises a close-out until the emit engine has drained to
// idle (no cross-batch status overtake). joined_count reports the rows emitted
// by the current/last probe batch (0 after a load or an error close-out).
module dau_int32_broadcast_hash_join #(
    // key space (slot count); one payload slot per key. The build side is a
    // broadcast table sized to fit BRAM. KEY_SPACE (and the payload width) are
    // synthesis parameters — a rebuild-to-silicon that needs a different key
    // space overrides this default.
    parameter int KEY_SPACE = 1024
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        cfg_load,      // 1 = load build rows, 0 = probe rows
    input  wire logic [2:0]  cfg_out0_sel,  // out0 join source: 0..5 -> {b0,b1,b2,v0,v1,v2}
    input  wire logic [2:0]  cfg_out1_sel,  // out1 join source
    input  wire logic [2:0]  cfg_out2_sel,  // out2 join source
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
    output logic [63:0]      joined_count
);
    localparam int AWIDTH = (KEY_SPACE <= 1) ? 1 : $clog2(KEY_SPACE);
    localparam logic [AWIDTH-1:0] LAST_ADDR = AWIDTH'(KEY_SPACE - 1);
    localparam logic [31:0] KEY_SPACE_U = KEY_SPACE;

    // table slot: {valid, b2, b1, b0}
    localparam int SLOTW = 97;

    localparam logic [2:0] SRC_MAX = 3'd5;  // sources 6, 7 are reserved

    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;
    localparam logic [7:0] ERR_RANGE  = 8'd3;

    typedef enum logic [3:0] {
        S_ARM,         // batch idle: the first word decides load vs probe (cfg latches)
        S_CLEAR,       // load: sweep every slot's valid bit to 0 before build rows
        S_LOAD_W0,     // load: accept a build row's w0 (key, b0), range-check the key
        S_LOAD_W1,     // load: accept the build row's w1 (b1, b2), write the slot
        S_PROBE_W0,    // probe: accept a row's w0, range-check, issue the table read
        S_PROBE_W1,    // probe: w1 in, hit known, emit/hold the join or drop
        S_FLUSH,       // probe: release the held final joined row tagged last
        S_FLUSH_DRAIN, // hold input closed until the flushed final beat drains, then re-arm
        S_DISCARD,     // drain a bad-config / out-of-range batch to input_last
        S_ERR_DRAIN,   // drain the in-flight emitted beat, then raise the latched error
        S_STATUS       // hold a close-out (success or error) until accepted
    } state_t;

    typedef enum logic [1:0] {
        EMIT_IDLE,
        EMIT_W0,
        EMIT_W1
    } emit_t;

    state_t state;
    emit_t  emit_state;

    // the direct-mapped payload table: KEY_SPACE x {valid, payload}, one write
    // port (LOAD/CLEAR) and one registered read port (PROBE), never both in a
    // cycle. ram_style=block keeps it BRAM-resident so it scales without
    // burning LUTs (the join tier's binding constraint is BRAM).
    (* ram_style = "block" *) logic [SLOTW-1:0] table_mem [0:KEY_SPACE-1];
    // MULTIDRIVEN is waived on this BRAM-output register ONLY. An `initial`
    // seeding state that `always_ff` then drives is how power-on values reach
    // the hardware (Vivado carries the INIT), and Verilator counts the two as
    // rival processes. Scoped to this declaration so a genuine second driver
    // anywhere else in the module is still an error.
    /* verilator lint_off MULTIDRIVEN */
    logic [SLOTW-1:0] read_slot;
    /* verilator lint_on MULTIDRIVEN */

    logic [AWIDTH-1:0] clear_addr;

    // batch-latched probe config (sampled at S_ARM so a mid-batch cfg change
    // cannot re-select the emitted fields)
    logic [2:0] active_out0;
    logic [2:0] active_out1;
    logic [2:0] active_out2;

    logic [63:0] row_w0;  // registered w0 of the row in flight (key + b0/v0)

    logic        held_valid;
    logic [63:0] held_w0;
    logic [63:0] held_w1;
    logic [63:0] emit_w0;
    logic [63:0] emit_w1;
    logic        emit_last;

    logic [7:0]  pending_err;  // error code latched while draining to a close-out

    logic input_fire;
    logic output_fire;
    logic emit_free;

    logic        cfg_out_ok;
    logic        key_in_range;
    logic [AWIDTH-1:0] probe_raddr;
    logic        probe_w0_accept;

    logic        mem_we;
    logic [AWIDTH-1:0] mem_waddr;
    logic [SLOTW-1:0]  mem_wdata;

    logic        hit;
    logic [31:0] join_out0;
    logic [31:0] join_out1;
    logic [31:0] join_out2;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign emit_free = (emit_state == EMIT_IDLE) || ((emit_state == EMIT_W1) && output_fire);

    always_comb begin
        unique case (state)
            S_ARM:       input_ready = !cfg_load;  // a load batch defers to the clear sweep
            S_PROBE_W1:  input_ready = (!held_valid || emit_free);
            S_LOAD_W0, S_LOAD_W1, S_PROBE_W0, S_DISCARD: input_ready = 1'b1;
            default:     input_ready = 1'b0;  // S_CLEAR, S_FLUSH, S_FLUSH_DRAIN, S_ERR_DRAIN, S_STATUS
        endcase
    end

    assign output_valid = emit_state != EMIT_IDLE;
    assign output_data = (emit_state == EMIT_W0) ? emit_w0 : emit_w1;
    assign output_last = (emit_state == EMIT_W1) && emit_last;

    // config validity: every output field-select must name a real source
    assign cfg_out_ok = (cfg_out0_sel <= SRC_MAX) && (cfg_out1_sel <= SRC_MAX) && (cfg_out2_sel <= SRC_MAX);

    // key range: the row key (w0.lo). A key outside [0, KEY_SPACE) fails the
    // unsigned range compare (negatives read as >= 2**31 > KEY_SPACE_U) and is
    // rejected; the address is clamped so a rejected key never indexes past the
    // table.
    assign key_in_range = (input_data[31:0] < KEY_SPACE_U);
    assign probe_raddr = key_in_range ? input_data[AWIDTH-1:0] : '0;

    // a probe w0 is being accepted this cycle and will advance to S_PROBE_W1
    // (the table read is issued here and lands in S_PROBE_W1); never for a
    // torn / out-of-range / bad-config first word
    assign probe_w0_accept = input_fire
        && (((state == S_ARM) && !cfg_load && cfg_out_ok && !input_last && key_in_range)
            || ((state == S_PROBE_W0) && !input_last && key_in_range));

    // table writes: the CLEAR sweep, then each accepted build row's w1
    always_comb begin
        mem_we = 1'b0;
        mem_waddr = '0;
        mem_wdata = '0;
        if (state == S_CLEAR) begin
            mem_we = 1'b1;
            mem_waddr = clear_addr;
            mem_wdata = '0;  // valid = 0
        end else if (input_fire && (state == S_LOAD_W1)) begin
            mem_we = 1'b1;
            mem_waddr = row_w0[AWIDTH-1:0];  // the build key (range-checked at S_LOAD_W0)
            mem_wdata = {1'b1, input_data[63:32], input_data[31:0], row_w0[63:32]};
        end
    end

    // the joined output row's data fields, selected from the build payload
    // (read_slot) and the probe payload (row_w0.hi = v0, input_data = v1|v2)
    assign hit = read_slot[96];
    function automatic logic [31:0] join_select(
        input logic [2:0]  sel,
        input logic [31:0] b0, input logic [31:0] b1, input logic [31:0] b2,
        input logic [31:0] v0, input logic [31:0] v1, input logic [31:0] v2
    );
        begin
            unique case (sel)
                3'd0: join_select = b0;
                3'd1: join_select = b1;
                3'd2: join_select = b2;
                3'd3: join_select = v0;
                3'd4: join_select = v1;
                default: join_select = v2;  // 3'd5
            endcase
        end
    endfunction

    assign join_out0 = join_select(active_out0, read_slot[31:0], read_slot[63:32], read_slot[95:64],
        row_w0[63:32], input_data[31:0], input_data[63:32]);
    assign join_out1 = join_select(active_out1, read_slot[31:0], read_slot[63:32], read_slot[95:64],
        row_w0[63:32], input_data[31:0], input_data[63:32]);
    assign join_out2 = join_select(active_out2, read_slot[31:0], read_slot[63:32], read_slot[95:64],
        row_w0[63:32], input_data[31:0], input_data[63:32]);

    // zero-init so a probe before any load is a well-defined empty table
    // (Vivado carries the INIT into the BRAM)
    initial begin
        for (int i = 0; i < KEY_SPACE; i++) table_mem[i] = '0;
        read_slot = '0;
    end

    always_ff @(posedge clk) begin
        if (mem_we) begin
            table_mem[mem_waddr] <= mem_wdata;
        end
        if (probe_w0_accept) begin
            read_slot <= table_mem[probe_raddr];
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

            case (state)
                S_ARM: begin
                    if (cfg_load) begin
                        // LOAD phase: sweep the valid bits, then accept build
                        // rows (input stays closed until the sweep completes)
                        if (input_valid) begin
                            joined_count <= 64'd0;
                            clear_addr <= '0;
                            state <= S_CLEAR;
                        end
                    end else if (input_fire) begin
                        // PROBE phase: the first row latches the field-select
                        active_out0 <= cfg_out0_sel;
                        active_out1 <= cfg_out1_sel;
                        active_out2 <= cfg_out2_sel;
                        joined_count <= 64'd0;
                        if (!cfg_out_ok) begin
                            // config precedence over stream shape: drain the
                            // batch, then CONFIG
                            pending_err <= ERR_CONFIG;
                            if (input_last) state <= S_ERR_DRAIN;  // emit idle: raises at once
                            else state <= S_DISCARD;
                        end else if (input_last) begin
                            raise_error(ERR_STREAM);  // torn probe row
                        end else if (!key_in_range) begin
                            pending_err <= ERR_RANGE;
                            state <= S_DISCARD;  // drain this row's w1 + the rest
                        end else begin
                            row_w0 <= input_data;  // read issued via probe_w0_accept
                            state <= S_PROBE_W1;
                        end
                    end
                end
                S_CLEAR: begin
                    // sweep addr 0..LAST_ADDR writing valid=0 (mem_we above)
                    if (clear_addr == LAST_ADDR) state <= S_LOAD_W0;
                    else clear_addr <= clear_addr + AWIDTH'(1);
                end
                S_LOAD_W0: begin
                    if (input_fire) begin
                        if (input_last) begin
                            raise_error(ERR_STREAM);  // torn build row
                        end else if (!key_in_range) begin
                            pending_err <= ERR_RANGE;
                            state <= S_DISCARD;  // drain this row's w1 + the rest
                        end else begin
                            row_w0 <= input_data;  // key + b0
                            state <= S_LOAD_W1;
                        end
                    end
                end
                S_LOAD_W1: begin
                    if (input_fire) begin
                        // the slot is written by mem_we this cycle
                        if (input_last) raise_success();  // load-done
                        else state <= S_LOAD_W0;
                    end
                end
                S_PROBE_W0: begin
                    if (input_fire) begin
                        if (input_last) begin
                            // torn row: the batch is invalid. If a joined beat
                            // is still in flight, drain it before the error
                            // close-out so status cannot overtake it.
                            if (emit_state == EMIT_IDLE) begin
                                raise_error(ERR_STREAM);
                            end else begin
                                pending_err <= ERR_STREAM;
                                held_valid <= 1'b0;
                                joined_count <= 64'd0;
                                state <= S_ERR_DRAIN;
                            end
                        end else if (!key_in_range) begin
                            pending_err <= ERR_RANGE;
                            state <= S_DISCARD;
                        end else begin
                            row_w0 <= input_data;
                            state <= S_PROBE_W1;
                        end
                    end
                end
                S_PROBE_W1: begin
                    if (input_fire) begin
                        if (hit) begin
                            if (held_valid) start_emit(held_w0, held_w1, 1'b0);
                            held_valid <= 1'b1;
                            held_w0 <= {join_out0, row_w0[31:0]};  // {out0, key}
                            held_w1 <= {join_out2, join_out1};     // {out2, out1}
                            joined_count <= joined_count + 64'd1;
                        end
                        if (input_last) begin
                            if (hit || held_valid) state <= S_FLUSH;
                            else raise_success();  // zero emitted rows: the compaction close-out
                        end else begin
                            state <= S_PROBE_W0;
                        end
                    end
                end
                S_FLUSH: begin
                    // release the held final joined row tagged last; do NOT
                    // re-arm yet (input stays closed via S_FLUSH_DRAIN) so a
                    // following batch's close-out cannot overtake this
                    // still-in-flight tail
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
                    if (emit_state == EMIT_IDLE) state <= S_ARM;
                end
                S_DISCARD: begin
                    // drain a bad-config / out-of-range batch to input_last,
                    // then raise the latched error once the emit engine drains
                    if (input_fire && input_last) begin
                        held_valid <= 1'b0;
                        joined_count <= 64'd0;
                        if (emit_state == EMIT_IDLE) raise_error(pending_err);
                        else state <= S_ERR_DRAIN;
                    end
                end
                S_ERR_DRAIN: begin
                    // hold input closed until the in-flight emitted beat has
                    // drained, then raise the latched error close-out
                    if (emit_state == EMIT_IDLE) raise_error(pending_err);
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
    // left to drain any in-flight beat normally (raise_error is only reached
    // with the emit engine idle — the drain states gate on it); the batch is
    // invalid, so the downstream aborts it on the error status.
    task automatic raise_error(input logic [7:0] code);
        begin
            held_valid <= 1'b0;
            joined_count <= 64'd0;
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
            clear_addr <= '0;
            active_out0 <= 3'd0;
            active_out1 <= 3'd0;
            active_out2 <= 3'd0;
            row_w0 <= 64'd0;
            held_valid <= 1'b0;
            held_w0 <= 64'd0;
            held_w1 <= 64'd0;
            emit_w0 <= 64'd0;
            emit_w1 <= 64'd0;
            emit_last <= 1'b0;
            pending_err <= 8'd0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            joined_count <= 64'd0;
        end
    endtask
endmodule

`default_nettype wire

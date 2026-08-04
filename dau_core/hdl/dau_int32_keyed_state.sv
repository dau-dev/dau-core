`default_nettype none

// Keyed state tile: the first stateful operator, and the owned storage
// class's first real consumer (STORAGE_AND_NOC.md). A per-key accumulator
// updated once per row, emitting the POST-update state for every row —
// matches dau_core.operators.keyed_state_scan bit-for-bit. This is the
// loop-carried-per-key shape a SIMD CPU cannot vectorize: state lives in a
// BRAM arena beside the compute and updates at on-chip bandwidth.
//
// One 64-bit word per row: {value[31:0], key[31:0]}. Output mirrors it:
// {post_update[31:0], key[31:0]}. The op (SET/ADD/MAX/MIN), the initial
// accumulator, and the KEY GRANT latch from config ports on each batch's
// first beat — host-written today, allocator-tile-driven tomorrow, the tile
// cannot tell.
//
// Implementation: hash (low key bits) + linear probe over a CAPACITY-slot
// {key, value} BRAM with a separate valid array. Deliberately SEQUENTIAL —
// one row in flight, so the read-modify-write hazard a pipelined design must
// forward around does not exist here. The declared ingest rate carries that
// honesty; the 1-row/cycle CAM/pipelined variant is the named follow-up.
//
// The GRANT bounds distinct keys (live_count), the parameter bounds slots.
// A new key past the grant is KEY_CAPACITY_EXCEEDED — a defined close-out
// preserving emitted rows (the shape a grow-request replaces when the
// allocator hardens), never an eviction: silently dropping a key would be
// wrong in a way no downstream consumer could detect.
//
// State is FRESH per batch, matching the golden: a CLEAR sweep over the
// valid array runs from BOTH reset and close-out, so a mid-batch reset
// cannot leak one batch's keys into the next.
module dau_int32_keyed_state #(
    parameter int CAPACITY = 1024  // key slots; [1, 1024]
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [1:0]  cfg_op,            // 0 SET, 1 ADD, 2 MAX, 3 MIN
    input  wire logic [31:0] cfg_initial,
    input  wire logic [31:0] cfg_granted_keys,
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
    output logic [63:0]      row_count
);
    localparam logic [7:0] ERR_BAD_GRANT = 8'd1;
    localparam logic [7:0] ERR_KEY_CAPACITY = 8'd2;
    localparam int AW = (CAPACITY <= 1) ? 1 : $clog2(CAPACITY);

    if (CAPACITY < 1 || CAPACITY > 1024) begin : g_param_check
        $error("CAPACITY must be in [1, 1024]");
    end

    typedef enum logic [2:0] {
        STATE_CLEAR,    // sweep the valid array (from reset and from close-out)
        STATE_ROW,      // accept {value, key}; latch config on a batch's first beat
        STATE_PROBE,    // present the probe index to the slot BRAM
        STATE_JUDGE,    // registered slot: match / empty / collide
        STATE_EMIT,     // present {post_update, key}
        STATE_DISCARD,  // error latched: consume the batch to input_last
        STATE_STATUS    // hold the close-out until accepted
    } state_t;

    state_t state;

    // {key, value} per slot; valid bits separate so the CLEAR sweep never
    // touches the wide BRAM
    (* ram_style = "block" *) logic [63:0] slots [0:CAPACITY-1];
    logic valid_bits [0:CAPACITY-1];

    logic        batch_open;
    logic [1:0]  op_q;
    logic [31:0] initial_q;
    logic [31:0] granted;
    logic [31:0] live_count;

    logic [31:0] cur_key;
    logic [31:0] cur_value;
    logic        cur_last;

    logic [AW-1:0] probe_idx;
    logic [31:0]   probes_done;
    logic [63:0]   slot_q;
    logic          valid_q;
    logic [31:0]   emit_value;
    logic [AW-1:0] clear_idx;
    logic          err_pending;
    logic [7:0]    err_code;

    assign input_ready = (state == STATE_ROW) || (state == STATE_DISCARD);

    // the post-update value: op(current-or-initial, row value), int32 wires
    function automatic logic [31:0] apply_op(input logic [1:0] op, input logic [31:0] current, input logic [31:0] value);
        unique case (op)
            2'd0: apply_op = value;                                                       // SET
            2'd1: apply_op = current + value;                                             // ADD (wraps)
            2'd2: apply_op = ($signed(current) > $signed(value)) ? current : value;       // MAX
            default: apply_op = ($signed(current) < $signed(value)) ? current : value;    // MIN
        endcase
    endfunction

    // slot BRAM: dedicated reset-free process (BRAM inference; the scratchpad
    // lesson). Read presents in PROBE, data lands for JUDGE. Writes happen in
    // JUDGE on a match or a granted new key.
    logic             slot_we;
    logic [AW-1:0]    slot_addr;
    logic [63:0]      slot_wdata;
    always_ff @(posedge clk) begin
        if (slot_we) begin
            slots[slot_addr] <= slot_wdata;
        end
        slot_q <= slots[slot_addr];
    end

    // judged outcomes (combinational off the registered slot read)
    logic hit;
    logic empty;
    assign hit = valid_q && (slot_q[31:0] == cur_key);
    assign empty = !valid_q;

    logic [31:0] updated;
    assign updated = hit ? apply_op(op_q, slot_q[63:32], cur_value)
                         : apply_op(op_q, initial_q, cur_value);

    assign slot_addr = probe_idx;
    assign slot_wdata = {updated, cur_key};
    assign slot_we = (state == STATE_JUDGE) && (hit || (empty && (live_count < granted)));

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= STATE_CLEAR;
            clear_idx <= '0;
            batch_open <= 1'b0;
            granted <= 32'd0;
            op_q <= 2'd0;
            initial_q <= 32'd0;
            live_count <= 32'd0;
            output_valid <= 1'b0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            err_pending <= 1'b0;
            err_code <= 8'd0;
            row_count <= 64'd0;
            cur_key <= 32'd0;
            cur_value <= 32'd0;
            cur_last <= 1'b0;
            probe_idx <= '0;
            probes_done <= 32'd0;
            valid_q <= 1'b0;
            emit_value <= 32'd0;
        end else begin
            case (state)
                STATE_CLEAR: begin
                    valid_bits[clear_idx] <= 1'b0;
                    if (clear_idx == AW'(CAPACITY - 1)) begin
                        clear_idx <= '0;
                        live_count <= 32'd0;
                        state <= STATE_ROW;
                    end else begin
                        clear_idx <= clear_idx + 1'b1;
                    end
                end
                STATE_ROW: begin
                    if (input_valid) begin
                        cur_key <= input_data[31:0];
                        cur_value <= input_data[63:32];
                        cur_last <= input_last;
                        probe_idx <= AW'(input_data[31:0] % CAPACITY);
                        probes_done <= 32'd0;
                        if (!batch_open) begin
                            batch_open <= 1'b1;
                            op_q <= cfg_op;
                            initial_q <= cfg_initial;
                            granted <= cfg_granted_keys;
                            if (cfg_granted_keys == 32'd0 || cfg_granted_keys > CAPACITY[31:0]) begin
                                err_pending <= 1'b1;
                                err_code <= ERR_BAD_GRANT;
                                state <= input_last ? STATE_STATUS : STATE_DISCARD;
                            end else begin
                                state <= STATE_PROBE;
                            end
                        end else begin
                            state <= STATE_PROBE;
                        end
                    end
                end
                STATE_PROBE: begin
                    // slot read presented this cycle; judged next. valid bit
                    // registered alongside so JUDGE sees a coherent pair.
                    valid_q <= valid_bits[probe_idx];
                    state <= STATE_JUDGE;
                end
                STATE_JUDGE: begin
                    if (hit) begin
                        emit_value <= updated;  // slot write happens this cycle
                        state <= STATE_EMIT;
                    end else if (empty) begin
                        if (live_count < granted) begin
                            valid_bits[probe_idx] <= 1'b1;
                            live_count <= live_count + 32'd1;
                            emit_value <= updated;
                            state <= STATE_EMIT;
                        end else begin
                            err_pending <= 1'b1;
                            err_code <= ERR_KEY_CAPACITY;
                            state <= cur_last ? STATE_STATUS : STATE_DISCARD;
                        end
                    end else begin
                        // collision: advance the probe (wrapping); a full scan
                        // without a hit or an empty slot means the table holds
                        // CAPACITY other keys — the grant cannot admit this one
                        if (probes_done + 32'd1 >= CAPACITY[31:0]) begin
                            err_pending <= 1'b1;
                            err_code <= ERR_KEY_CAPACITY;
                            state <= cur_last ? STATE_STATUS : STATE_DISCARD;
                        end else begin
                            probes_done <= probes_done + 32'd1;
                            probe_idx <= (probe_idx == AW'(CAPACITY - 1)) ? '0 : probe_idx + 1'b1;
                            state <= STATE_PROBE;
                        end
                    end
                end
                STATE_EMIT: begin
                    output_valid <= 1'b1;
                    output_data <= {emit_value, cur_key};
                    output_last <= cur_last;
                    if (output_valid && output_ready) begin
                        output_valid <= 1'b0;
                        output_last <= 1'b0;
                        row_count <= row_count + 64'd1;
                        if (cur_last) begin
                            status_valid <= 1'b1;
                            status_error <= 1'b0;
                            status_error_code <= 8'd0;
                            state <= STATE_STATUS;
                        end else begin
                            state <= STATE_ROW;
                        end
                    end
                end
                STATE_DISCARD: begin
                    if (input_valid && input_last) begin
                        state <= STATE_STATUS;
                    end
                end
                STATE_STATUS: begin
                    if (err_pending && !status_valid) begin
                        status_valid <= 1'b1;
                        status_error <= 1'b1;
                        status_error_code <= err_code;
                    end
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        err_pending <= 1'b0;
                        batch_open <= 1'b0;
                        row_count <= 64'd0;
                        state <= STATE_CLEAR;  // fresh state per batch, matching the golden
                    end
                end
                default: state <= STATE_CLEAR;
            endcase
        end
    end
endmodule

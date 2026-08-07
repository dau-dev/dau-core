`default_nettype none

// Session-gap tile: the Nexmark-Q11 shape over bounded keyed state
// (STORAGE_AND_NOC.md). Per-key sessions over time-ordered rows: a row
// whose time exceeds its key's last by more than cfg_gap CLOSES the old
// session (emitted immediately, arrival order) and opens a new one;
// batch end drains everything still open SORTED BY SIGNED KEY — repeated
// minimum-key extraction, so the emission order is the golden's contract
// and no hash placement or slot index can leak into it. Matches
// dau_core.operators.sessionize_scan bit-for-bit.
//
// One 64-bit word in per row: {time[31:0], key[31:0]}. Each CLOSED session
// emits two words: w0 = {start[31:0], key[31:0]}, w1 = {count[31:0],
// last[31:0]}.
//
// Scope is PROCESSING-order sessionization: times must be non-decreasing
// across the batch, and a violation is TIME_ORDER — refused, never silently
// reordered. Event time, watermarks and late arrivals are deliberately
// absent. Error close-outs (TIME_ORDER, SESSION_CAPACITY_EXCEEDED, BAD_*)
// preserve already-emitted
// sessions and skip the drain, matching the golden; the CLEAR sweep
// retires their leftover state.
//
// The state table composes the keyed-state pattern: hash (low key bits) +
// linear probe over a CAPACITY-slot {key, start, last, count} BRAM with a
// separate valid array, sequential (one row in flight, no RMW hazard), the
// grant bounding DISTINCT KEYS per batch (a gap-close immediately reopens,
// so it never frees a slot mid-batch). The gap compare runs in 33-bit
// signed arithmetic: times are int32 wires and their difference spans
// +/- 2^32, exactly the golden's unbounded-integer compare.
//
// The drain costs O(CAPACITY x live_sessions) cycles at close-out —
// bounded, batch-tail amortized per the IngestRate doctrine, and the price
// of an implementation-independent emission order. A systolic sorted drain
// (the top-k array) is the named follow-up if a workload needs it.
module dau_int32_session_gap #(
    parameter int CAPACITY = 1024  // session slots; [1, 1024]
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [31:0] cfg_gap,
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
    output logic [63:0]      session_count
);
    localparam logic [7:0] ERR_BAD_GRANT = 8'd1;
    localparam logic [7:0] ERR_SESSION_CAPACITY = 8'd2;
    localparam logic [7:0] ERR_BAD_GAP = 8'd3;
    localparam logic [7:0] ERR_TIME_ORDER = 8'd4;
    localparam int AW = (CAPACITY <= 1) ? 1 : $clog2(CAPACITY);

    if (CAPACITY < 1 || CAPACITY > 1024) begin : g_param_check
        $error("CAPACITY must be in [1, 1024]");
    end

    typedef enum logic [3:0] {
        STATE_CLEAR,       // sweep the valid array (from reset and close-out)
        STATE_ROW,         // accept {time, key}; latch config on first beat
        STATE_PROBE,       // present the probe index to the slot BRAM
        STATE_JUDGE,       // match / empty / collide; gap decision on match
        STATE_EMIT_W0,     // closed session word 0: {start, key}
        STATE_EMIT_W1,     // closed session word 1: {count, last}
        STATE_DRAIN_SCAN,  // find the minimum signed key among open sessions
        STATE_DRAIN_READ,  // fold the final scanned slot into the minimum
        STATE_DRAIN_ADDR,  // present the winner's index to the slot BRAM
        STATE_DRAIN_JUDGE, // register the winner's data
        STATE_DISCARD,     // error latched: consume the batch to input_last
        STATE_STATUS       // hold the close-out until accepted
    } state_t;

    state_t state;

    // {count, last, start, key} per slot
    (* ram_style = "block" *) logic [127:0] slots [0:CAPACITY-1];
    logic valid_bits [0:CAPACITY-1];

    logic        batch_open;
    logic [31:0] gap_q;
    logic [31:0] granted;
    logic [31:0] live_count;
    logic [31:0] prev_time;
    logic        prev_time_set;

    logic [31:0] cur_key;
    logic [31:0] cur_time;
    logic        cur_last;

    logic [AW-1:0] probe_idx;
    logic [31:0]   probes_done;
    logic [127:0]  slot_q;
    logic          valid_q;

    // the closed session being emitted (from a gap split or the drain)
    logic [31:0] emit_key;
    logic [31:0] emit_start;
    logic [31:0] emit_last_t;
    logic [31:0] emit_count;
    logic        emit_from_drain;
    logic        row_was_last;  // the split row was the batch's last

    // drain scan
    logic [AW-1:0] scan_idx;
    logic [AW-1:0] min_idx;
    logic [31:0]   min_key;
    logic          min_found;
    logic          scan_slot_valid;

    logic [AW-1:0] clear_idx;
    logic          err_pending;
    logic [7:0]    err_code;

    assign input_ready = (state == STATE_ROW) || (state == STATE_DISCARD);

    // 33-bit signed gap compare: time - last > gap, exact over int32 wires
    logic signed [32:0] time_minus_last;
    assign time_minus_last = $signed({cur_time[31], cur_time}) - $signed({slot_q[95], slot_q[95:64]});
    logic gap_exceeded;
    assign gap_exceeded = time_minus_last > $signed({1'b0, gap_q});

    // slot BRAM: dedicated reset-free process (the BRAM-inference lesson)
    logic           slot_we;
    logic [AW-1:0]  slot_addr;
    logic [127:0]   slot_wdata;
    always_ff @(posedge clk) begin
        if (slot_we) begin
            slots[slot_addr] <= slot_wdata;
        end
        slot_q <= slots[slot_addr];
    end

    logic hit;
    logic empty;
    assign hit = valid_q && (slot_q[31:0] == cur_key);
    assign empty = !valid_q;

    // a matched row either extends {start, time, count+1} or reopens
    // {time, time, 1}; a new key opens {time, time, 1}
    logic [127:0] extended;
    logic [127:0] reopened;
    assign extended = {slot_q[127:96] + 32'd1, cur_time, slot_q[63:32], cur_key};
    assign reopened = {32'd1, cur_time, cur_time, cur_key};

    assign slot_addr = (state == STATE_DRAIN_SCAN) ? scan_idx
                     : (state == STATE_DRAIN_ADDR || state == STATE_DRAIN_JUDGE) ? min_idx
                     : probe_idx;
    assign slot_wdata = (hit && gap_exceeded) ? reopened : (hit ? extended : reopened);
    assign slot_we = (state == STATE_JUDGE) && (hit || (empty && (live_count < granted)));

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= STATE_CLEAR;
            clear_idx <= '0;
            batch_open <= 1'b0;
            gap_q <= 32'd0;
            granted <= 32'd0;
            live_count <= 32'd0;
            prev_time <= 32'd0;
            prev_time_set <= 1'b0;
            output_valid <= 1'b0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            err_pending <= 1'b0;
            err_code <= 8'd0;
            session_count <= 64'd0;
            cur_key <= 32'd0;
            cur_time <= 32'd0;
            cur_last <= 1'b0;
            probe_idx <= '0;
            probes_done <= 32'd0;
            valid_q <= 1'b0;
            emit_key <= 32'd0;
            emit_start <= 32'd0;
            emit_last_t <= 32'd0;
            emit_count <= 32'd0;
            emit_from_drain <= 1'b0;
            row_was_last <= 1'b0;
            scan_idx <= '0;
            min_idx <= '0;
            min_key <= 32'd0;
            min_found <= 1'b0;
            scan_slot_valid <= 1'b0;
        end else begin
            case (state)
                STATE_CLEAR: begin
                    valid_bits[clear_idx] <= 1'b0;
                    if (clear_idx == AW'(CAPACITY - 1)) begin
                        clear_idx <= '0;
                        live_count <= 32'd0;
                        prev_time_set <= 1'b0;
                        state <= STATE_ROW;
                    end else begin
                        clear_idx <= clear_idx + 1'b1;
                    end
                end
                STATE_ROW: begin
                    if (input_valid) begin
                        cur_key <= input_data[31:0];
                        cur_time <= input_data[63:32];
                        cur_last <= input_last;
                        probe_idx <= AW'(input_data[31:0] % CAPACITY);
                        probes_done <= 32'd0;
                        if (!batch_open) begin
                            batch_open <= 1'b1;
                            gap_q <= cfg_gap;
                            granted <= cfg_granted_keys;
                            // grant before gap: the golden checks capacity
                            // first, and when BOTH are invalid the codes must
                            // agree bit-for-bit
                            if (cfg_granted_keys == 32'd0 || cfg_granted_keys > CAPACITY[31:0]) begin
                                err_pending <= 1'b1;
                                err_code <= ERR_BAD_GRANT;
                                state <= input_last ? STATE_STATUS : STATE_DISCARD;
                            end else if (cfg_gap == 32'd0 || cfg_gap[31]) begin
                                err_pending <= 1'b1;
                                err_code <= ERR_BAD_GAP;
                                state <= input_last ? STATE_STATUS : STATE_DISCARD;
                            end else begin
                                prev_time <= input_data[63:32];
                                prev_time_set <= 1'b1;
                                state <= STATE_PROBE;
                            end
                        end else if (prev_time_set && ($signed(input_data[63:32]) < $signed(prev_time))) begin
                            err_pending <= 1'b1;
                            err_code <= ERR_TIME_ORDER;
                            state <= input_last ? STATE_STATUS : STATE_DISCARD;
                        end else begin
                            prev_time <= input_data[63:32];
                            state <= STATE_PROBE;
                        end
                    end
                end
                STATE_PROBE: begin
                    valid_q <= valid_bits[probe_idx];
                    state <= STATE_JUDGE;
                end
                STATE_JUDGE: begin
                    if (hit) begin
                        if (gap_exceeded) begin
                            // the old session closes (slot reopens this cycle)
                            emit_key <= slot_q[31:0];
                            emit_start <= slot_q[63:32];
                            emit_last_t <= slot_q[95:64];
                            emit_count <= slot_q[127:96];
                            emit_from_drain <= 1'b0;
                            row_was_last <= cur_last;
                            state <= STATE_EMIT_W0;
                        end else begin
                            state <= cur_last ? STATE_DRAIN_SCAN : STATE_ROW;
                            scan_idx <= '0;
                            min_found <= 1'b0;
                        end
                    end else if (empty) begin
                        if (live_count < granted) begin
                            valid_bits[probe_idx] <= 1'b1;
                            live_count <= live_count + 32'd1;
                            state <= cur_last ? STATE_DRAIN_SCAN : STATE_ROW;
                            scan_idx <= '0;
                            min_found <= 1'b0;
                        end else begin
                            err_pending <= 1'b1;
                            err_code <= ERR_SESSION_CAPACITY;
                            state <= cur_last ? STATE_STATUS : STATE_DISCARD;
                        end
                    end else begin
                        if (probes_done + 32'd1 >= CAPACITY[31:0]) begin
                            err_pending <= 1'b1;
                            err_code <= ERR_SESSION_CAPACITY;
                            state <= cur_last ? STATE_STATUS : STATE_DISCARD;
                        end else begin
                            probes_done <= probes_done + 32'd1;
                            probe_idx <= (probe_idx == AW'(CAPACITY - 1)) ? '0 : probe_idx + 1'b1;
                            state <= STATE_PROBE;
                        end
                    end
                end
                STATE_EMIT_W0: begin
                    output_valid <= 1'b1;
                    output_data <= {emit_start, emit_key};
                    output_last <= 1'b0;
                    if (output_valid && output_ready) begin
                        output_valid <= 1'b0;
                        state <= STATE_EMIT_W1;
                    end
                end
                STATE_EMIT_W1: begin
                    output_valid <= 1'b1;
                    output_data <= {emit_count, emit_last_t};
                    // the last word of the batch's final drained session
                    output_last <= emit_from_drain && (live_count == 32'd1);
                    if (output_valid && output_ready) begin
                        output_valid <= 1'b0;
                        output_last <= 1'b0;
                        session_count <= session_count + 64'd1;
                        if (emit_from_drain) begin
                            valid_bits[min_idx] <= 1'b0;
                            live_count <= live_count - 32'd1;
                            if (live_count == 32'd1) begin
                                status_valid <= 1'b1;
                                status_error <= 1'b0;
                                status_error_code <= 8'd0;
                                state <= STATE_STATUS;
                            end else begin
                                scan_idx <= '0;
                                min_found <= 1'b0;
                                state <= STATE_DRAIN_SCAN;
                            end
                        end else begin
                            state <= row_was_last ? STATE_DRAIN_SCAN : STATE_ROW;
                            scan_idx <= '0;
                            min_found <= 1'b0;
                        end
                    end
                end
                STATE_DRAIN_SCAN: begin
                    // slot_addr = scan_idx; slot_q lags by one cycle, so this
                    // cycle folds the PREVIOUS slot's {valid, key} pair
                    scan_slot_valid <= valid_bits[scan_idx];
                    if (scan_idx != '0) begin
                        if (scan_slot_valid && (!min_found || ($signed(slot_q[31:0]) < $signed(min_key)))) begin
                            min_found <= 1'b1;
                            min_key <= slot_q[31:0];
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
                    if (scan_slot_valid && (!min_found || ($signed(slot_q[31:0]) < $signed(min_key)))) begin
                        min_found <= 1'b1;
                        min_key <= slot_q[31:0];
                        min_idx <= AW'(CAPACITY - 1);
                    end
                    state <= STATE_DRAIN_ADDR;
                end
                STATE_DRAIN_ADDR: begin
                    // min_idx settled last cycle; slot_addr presents it now and
                    // slot_q holds the winner one cycle later
                    state <= STATE_DRAIN_JUDGE;
                end
                STATE_DRAIN_JUDGE: begin
                    // slot_q now holds the winner (slot_addr = min_idx)
                    emit_key <= slot_q[31:0];
                    emit_start <= slot_q[63:32];
                    emit_last_t <= slot_q[95:64];
                    emit_count <= slot_q[127:96];
                    emit_from_drain <= 1'b1;
                    state <= STATE_EMIT_W0;
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
                        session_count <= 64'd0;
                        emit_from_drain <= 1'b0;
                        clear_idx <= '0;
                        state <= STATE_CLEAR;
                    end
                end
                default: state <= STATE_CLEAR;
            endcase
        end
    end
endmodule

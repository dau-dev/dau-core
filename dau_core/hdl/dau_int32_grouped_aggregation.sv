`default_nettype none

`include "types.svh"

// Ordered grouped-aggregation tile (the Q100 grouped-aggregate
// shape, close-out mode).
//
// Consumes one keyed row per 64-bit word ({value, key}, lanes 0 and 1 of the
// word at FIELD_WIDTH each) and computes first/last/min/max (the element
// type) and sum/count (INT64) for each run of consecutive equal keys. A group
// closes when the key changes or the stream ends; close-out emits a 5-word
// group record whose slots do NOT move with the element type -- an element
// statistic rides a 32-bit slot sign-extended, an accumulator a 64-bit word:
//
//   w0 = {first, key}          (two 32-bit slots)
//   w1 = {minimum, last}       (two 32-bit slots)
//   w2 = {32'd0, maximum}
//   w3 = sum[63:0]     (INT64 accumulator over the element values)
//   w4 = count[63:0]
//
// A key that reappears after other keys opens a NEW group — partial groups
// from separate batches merge on the host, never in the tile (the
// partial-bar contract; first/last follow input order). Ordered mode holds
// exactly one live group, so there is no key table (hash mode for unordered
// input is a follow-on and rejects with CONFIG until then: cfg_mode must be
// zero). Input stalls while a record drains; a consumer must be attached.
// cfg_row_count = N checks the stream length; cfg_row_count = 0 selects
// last-driven mode (stages downstream of a compaction trust the upstream
// last marker). Configuration ports are latched at the first beat, matching
// dau_core.operators.grouped_aggregate_rows bit-for-bit.
module dau_int32_grouped_aggregation #(
    // The ELEMENT TYPE of the key and value lanes, as encoded by types.svh.
    // The 64-bit keyed word is the transport unit and stays fixed; lane i is
    // its i-th FIELD_BITS lane, so `DAU_TYPE_INT32 is 32 and the default
    // reproduces the {value, key} word the tile has always read. The record's
    // element slots are 32 bits and its accumulators 64, which is why the
    // type may narrow but not widen.
    parameter int FIELD_WIDTH = `DAU_TYPE_INT32
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [1:0]  cfg_mode,
    input  wire logic [63:0] cfg_row_count,
    input  wire logic        input_valid,
    output logic             input_ready,
    // only lanes 0 and 1 -- the key and the value -- are read. At the default
    // int32 those are the whole word; a narrower element leaves the lanes
    // above them reserved and unread (the streaming dedup says the same).
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
    output logic [63:0]      group_count
);
    localparam logic [1:0] MODE_ORDERED = 2'd0;

    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;

    localparam int WORD_BITS = 64;  // the transport unit, fixed
    localparam int FIELD_BITS      = `DAU_TYPE_WIDTH(FIELD_WIDTH);
    localparam int FIELD_IS_FLOAT  = `DAU_TYPE_IS_FLOAT(FIELD_WIDTH);
    localparam int FIELD_IS_SIGNED = `DAU_TYPE_IS_SIGNED(FIELD_WIDTH);
    localparam int SLOT_BITS = 32;  // the record's element slot, fixed with the record

    // The upper bound is the RECORD, not the lane: first/last/min/max ride
    // 32-bit slots and the sum a 64-bit word, so a wider element would need a
    // versioned record, i.e. a different tile. Narrowing is free.
    if (FIELD_BITS < 8 || FIELD_BITS > 32 || (WORD_BITS % FIELD_BITS) != 0) begin : g_field_width_check
        $error("FIELD_WIDTH must divide 64 and lie in [8, 32]");
    end
    // A TYPE IS NOT A CAPABILITY. This datapath ORDERS values (min/max) and
    // sign-extends them into a two's-complement INT64 accumulator: handed a
    // float it would order exponent bits as magnitude, and handed an unsigned
    // type it would read every high-bit-set value as negative.
    if (FIELD_IS_FLOAT != 0) begin : g_field_float_check
        $error("FIELD_WIDTH must not name a floating-point type: this tile orders and sums two's-complement integers");
    end
    if (FIELD_IS_SIGNED == 0) begin : g_field_signed_check
        $error("FIELD_WIDTH must not name an unsigned type: this tile orders and sums two's-complement integers");
    end

    typedef enum logic [3:0] {
        STATE_IDLE,
        STATE_STREAM,
        STATE_EMIT_W0,
        STATE_EMIT_W1,
        STATE_EMIT_W2,
        STATE_EMIT_W3,
        STATE_EMIT_W4,
        STATE_DRAIN_ERROR,
        STATE_STATUS_PEND,
        STATE_STATUS
    } state_t;

    state_t state;

    logic        count_checked;
    logic [63:0] rows_remaining;
    logic        group_open;
    logic signed [FIELD_BITS-1:0] group_key;
    logic signed [FIELD_BITS-1:0] group_first;
    logic signed [FIELD_BITS-1:0] group_last;
    logic signed [FIELD_BITS-1:0] group_min;
    logic signed [FIELD_BITS-1:0] group_max;
    logic signed [63:0] group_sum;
    logic [63:0] group_rows;
    logic        final_after_emit;
    logic        pending_row_valid;
    logic signed [FIELD_BITS-1:0] pending_key;
    logic signed [FIELD_BITS-1:0] pending_value;
    logic        pending_row_final;
    logic [7:0]  pending_error_code;

    logic input_fire;
    logic output_fire;
    logic status_fire;
    logic config_ok;
    logic final_word;
    logic key_change;

    logic [63:0] eff_remaining;
    logic        eff_count_checked;
    logic signed [FIELD_BITS-1:0] row_key;
    logic signed [FIELD_BITS-1:0] row_value;

    assign eff_remaining = (state == STATE_IDLE) ? cfg_row_count : rows_remaining;
    assign eff_count_checked = (state == STATE_IDLE) ? (|cfg_row_count) : count_checked;

    assign input_ready = ((state == STATE_IDLE) || (state == STATE_STREAM) || (state == STATE_DRAIN_ERROR))
        && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    assign config_ok = cfg_mode == MODE_ORDERED;

    // count-checked: registered down-counter, compare-to-constant only;
    // last-driven (row_count 0): the upstream last marker decides
    assign final_word = eff_count_checked ? ((~|eff_remaining[63:1]) && eff_remaining[0]) : input_last;

    assign row_key = signed'(input_data[0*FIELD_BITS +: FIELD_BITS]);
    assign row_value = signed'(input_data[1*FIELD_BITS +: FIELD_BITS]);
    assign key_change = group_open && (row_key != group_key);

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_IDLE, STATE_STREAM: begin
                    if (input_fire) begin
                        if ((state == STATE_IDLE) && !config_ok) begin
                            fail(ERR_CONFIG);
                        end else if (eff_count_checked && (input_last != final_word)) begin
                            if (input_last) begin
                                start_error_status(ERR_STREAM);
                            end else begin
                                fail(ERR_STREAM);
                            end
                        end else begin
                            if (state == STATE_IDLE) begin
                                count_checked <= |cfg_row_count;
                            end
                            rows_remaining <= eff_remaining - 64'd1;
                            if (key_change) begin
                                // close the live group; the arriving row is
                                // held and opens the next group after emit
                                pending_row_valid <= 1'b1;
                                pending_key <= row_key;
                                pending_value <= row_value;
                                pending_row_final <= final_word;
                                final_after_emit <= 1'b0;
                                state <= STATE_EMIT_W0;
                            end else begin
                                absorb_row(row_key, row_value);
                                if (final_word) begin
                                    final_after_emit <= 1'b1;
                                    state <= STATE_EMIT_W0;
                                end else begin
                                    state <= STATE_STREAM;
                                end
                            end
                        end
                    end
                end
                STATE_EMIT_W0: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= {SLOT_BITS'(signed'(group_first)), SLOT_BITS'(signed'(group_key))};
                        output_last <= 1'b0;
                        state <= STATE_EMIT_W1;
                    end
                end
                STATE_EMIT_W1: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= {SLOT_BITS'(signed'(group_min)), SLOT_BITS'(signed'(group_last))};
                        output_last <= 1'b0;
                        state <= STATE_EMIT_W2;
                    end
                end
                STATE_EMIT_W2: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= {32'd0, SLOT_BITS'(signed'(group_max))};
                        output_last <= 1'b0;
                        state <= STATE_EMIT_W3;
                    end
                end
                STATE_EMIT_W3: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= unsigned'(group_sum);
                        output_last <= 1'b0;
                        state <= STATE_EMIT_W4;
                    end
                end
                STATE_EMIT_W4: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= group_rows;
                        output_last <= final_after_emit && !pending_row_valid;
                        group_count <= group_count + 64'd1;
                        group_open <= 1'b0;
                        if (pending_row_valid) begin
                            // the held row opens the next group
                            // (unconditionally: group_open still reads the
                            // closing group's pre-edge value here)
                            open_group(pending_key, pending_value);
                            pending_row_valid <= 1'b0;
                            if (pending_row_final) begin
                                final_after_emit <= 1'b1;
                                state <= STATE_EMIT_W0;
                            end else begin
                                state <= STATE_STREAM;
                            end
                        end else begin
                            state <= STATE_STATUS_PEND;
                        end
                    end
                end
                STATE_DRAIN_ERROR: begin
                    if (input_fire && input_last) begin
                        start_error_status(pending_error_code);
                    end
                end
                STATE_STATUS_PEND: begin
                    if (!output_valid || output_ready) begin
                        status_valid <= 1'b1;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= STATE_STATUS;
                    end
                end
                STATE_STATUS: begin
                    if (status_fire) begin
                        reset_stream_state();
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    task automatic open_group(input logic signed [FIELD_BITS-1:0] key, input logic signed [FIELD_BITS-1:0] value);
        begin
            group_open <= 1'b1;
            group_key <= key;
            group_first <= value;
            group_last <= value;
            group_min <= value;
            group_max <= value;
            group_sum <= 64'(value);
            group_rows <= 64'd1;
        end
    endtask

    task automatic absorb_row(input logic signed [FIELD_BITS-1:0] key, input logic signed [FIELD_BITS-1:0] value);
        begin
            if (!group_open) begin
                open_group(key, value);
            end else begin
                group_last <= value;
                if (value < group_min) begin
                    group_min <= value;
                end
                if (value > group_max) begin
                    group_max <= value;
                end
                group_sum <= group_sum + 64'(value);
                group_rows <= group_rows + 64'd1;
            end
        end
    endtask

    task automatic fail(input logic [7:0] code);
        begin
            pending_error_code <= code;
            if (input_last) begin
                start_error_status(code);
            end else begin
                state <= STATE_DRAIN_ERROR;
            end
        end
    endtask

    task automatic start_error_status(input logic [7:0] code);
        begin
            output_valid <= 1'b0;
            output_last <= 1'b0;
            pending_row_valid <= 1'b0;
            group_open <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_stream_state;
        begin
            state <= STATE_IDLE;
            count_checked <= 1'b0;
            rows_remaining <= 64'd0;
            group_open <= 1'b0;
            final_after_emit <= 1'b0;
            pending_row_valid <= 1'b0;
            pending_row_final <= 1'b0;
            pending_error_code <= 8'd0;
            group_count <= 64'd0;
            output_valid <= 1'b0;
            output_data <= 64'd0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask

    task automatic reset_state;
        begin
            reset_stream_state();
            group_key <= '0;
            group_first <= '0;
            group_last <= '0;
            group_min <= '0;
            group_max <= '0;
            group_sum <= 64'd0;
            group_rows <= 64'd0;
            pending_key <= '0;
            pending_value <= '0;
        end
    endtask
endmodule

`default_nettype wire

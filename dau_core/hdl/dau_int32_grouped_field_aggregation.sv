`default_nettype none

`include "types.svh"

// Generic multi-field grouped-aggregation tile (ordered close-out): over quad
// rows (key, v0, v1, v2 -- the row's first four FIELD_WIDTH lanes) grouped by
// consecutive-equal key, computes for EACH of
// the three data fields its first / last / min / max / sum (INT64) in one pass,
// plus the row count. Workload-agnostic — OHLCV bars are a field/stat SELECTION
// over this record (open/high/low/close = field0 first/max/min/last, volume =
// sum field1, ...), not a baked-in tile; the finance column assignment lives in
// the host / a downstream select. Matches
// dau_core.operators.grouped_field_aggregate_rows bit-for-bit.
//
// Two 64-bit words per row (a row is atomic; input_last only legal on w1);
// the four lanes are read out of the ASSEMBLED 128-bit row {w1, w0}, which at
// the default int32 is exactly:
//   w0 = {v0[31:0], key[31:0]}
//   w1 = {v2[31:0], v1[31:0]}
// A group closes when the key changes or the stream ends; a reappearing key
// opens a NEW partial group (the host merges — the partial-group contract).
// Close-out emits an 11-word record whose slots do NOT move with the element
// type -- an element statistic rides a 32-bit slot sign-extended, an
// accumulator a 64-bit word:
//   r0  = {32'd0, key}
//   r1  = {f0_last, f0_first}     r2  = {f0_max, f0_min}     r3  = f0_sum[63:0]
//   r4  = {f1_last, f1_first}     r5  = {f1_max, f1_min}     r6  = f1_sum[63:0]
//   r7  = {f2_last, f2_first}     r8  = {f2_max, f2_min}     r9  = f2_sum[63:0]
//   r10 = count[63:0]
// A row torn by input_last on w0 is ERR_STREAM.
module dau_int32_grouped_field_aggregation #(
    // The ELEMENT TYPE of one lane, as encoded by types.svh. The 128-bit row
    // is the transport unit and stays fixed; lane i is its i-th FIELD_BITS
    // lane, the decode row-unpack packs and the quad-row filter family reads.
    // This tile keeps the key plus DATA_FIELDS data lanes and ignores any a
    // narrower type leaves beyond them -- the lane count is what the five
    // statistics per field cost. `DAU_TYPE_INT32 is 32, so the default
    // reproduces the (key, v0, v1, v2) row port for port. The record's element
    // slots are 32 bits and its accumulators 64, which is why the type may
    // narrow but not widen.
    parameter int FIELD_WIDTH = `DAU_TYPE_INT32
) (
    input  wire logic        clk,
    input  wire logic        rst,
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
    localparam logic [7:0] ERR_STREAM = 8'd2;

    localparam int ROW_BITS    = 128;  // the transport unit, fixed
    localparam int DATA_FIELDS = 3;    // the data lanes; lane 0 is the key
    localparam int FIELDS      = DATA_FIELDS + 1;
    localparam int FIELD_BITS      = `DAU_TYPE_WIDTH(FIELD_WIDTH);
    localparam int FIELD_IS_FLOAT  = `DAU_TYPE_IS_FLOAT(FIELD_WIDTH);
    localparam int FIELD_IS_SIGNED = `DAU_TYPE_IS_SIGNED(FIELD_WIDTH);
    localparam int LANE_BITS       = FIELDS * FIELD_BITS;  // the part of the row this tile reads
    localparam int DATA_BITS       = DATA_FIELDS * FIELD_BITS;  // its data lanes, key excluded
    localparam int SLOT_BITS       = 32;  // the record's element slot, fixed with the record

    // The upper bound is the RECORD, not the lane: first/last/min/max ride
    // 32-bit slots and each sum a 64-bit word, so a wider element would need a
    // versioned record, i.e. a different tile. Narrowing is free.
    if (FIELD_BITS < 8 || FIELD_BITS > 32 || (ROW_BITS % FIELD_BITS) != 0) begin : g_field_width_check
        $error("FIELD_WIDTH must divide 128 and lie in [8, 32]");
    end
    // A TYPE IS NOT A CAPABILITY. This datapath ORDERS values (min/max) and
    // sign-extends them into two's-complement INT64 accumulators: handed a
    // float it would order exponent bits as magnitude, and handed an unsigned
    // type it would read every high-bit-set value as negative.
    if (FIELD_IS_FLOAT != 0) begin : g_field_float_check
        $error("FIELD_WIDTH must not name a floating-point type: this tile orders and sums two's-complement integers");
    end
    if (FIELD_IS_SIGNED == 0) begin : g_field_signed_check
        $error("FIELD_WIDTH must not name an unsigned type: this tile orders and sums two's-complement integers");
    end

    typedef enum logic [4:0] {
        STATE_ROW_LOW,
        STATE_ROW_HIGH,
        STATE_PENDING_HIGH,
        STATE_EMIT_R0,
        STATE_EMIT_R1,
        STATE_EMIT_R2,
        STATE_EMIT_R3,
        STATE_EMIT_R4,
        STATE_EMIT_R5,
        STATE_EMIT_R6,
        STATE_EMIT_R7,
        STATE_EMIT_R8,
        STATE_EMIT_R9,
        STATE_EMIT_R10,
        STATE_DRAIN_ERROR,
        STATE_STATUS
    } state_t;

    state_t state;

    logic        grp_open;
    logic signed [FIELD_BITS-1:0] grp_key;
    // per field: first / last / min / max / sum(INT64)
    logic signed [FIELD_BITS-1:0] f_first [DATA_FIELDS];
    logic signed [FIELD_BITS-1:0] f_last  [DATA_FIELDS];
    logic signed [FIELD_BITS-1:0] f_min   [DATA_FIELDS];
    logic signed [FIELD_BITS-1:0] f_max   [DATA_FIELDS];
    logic signed [63:0] f_sum   [DATA_FIELDS];
    logic [63:0] grp_rows;

    logic        final_after_emit;
    // w0 is held WHOLE rather than as its two decoded lanes: at a narrower
    // element the later lanes sit inside w0 too, so the decode has to run on
    // the assembled row and not on either word alone.
    logic [63:0] row_w0;
    logic        held_is_pending;

    logic input_fire;
    logic output_fire;
    logic status_fire;
    logic final_row;
    logic key_change;

    logic signed [FIELD_BITS-1:0] row_key;
    logic [LANE_BITS-1:0] row_lanes;

    assign input_ready = ((state == STATE_ROW_LOW) || (state == STATE_ROW_HIGH)
        || (state == STATE_PENDING_HIGH) || (state == STATE_DRAIN_ERROR))
        && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    assign final_row = input_last;
    // lane 0 is always inside w0, so the key compare reads the incoming word
    // directly; the data lanes come from the assembled row
    assign row_key = signed'(input_data[0*FIELD_BITS +: FIELD_BITS]);
    assign row_lanes = LANE_BITS'({input_data, row_w0});
    assign key_change = grp_open && (row_key != grp_key);

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_ROW_LOW: begin
                    if (input_fire) begin
                        if (input_last) begin
                            // a row is two words: last on w0 truncates the row
                            start_error_status(ERR_STREAM);
                        end else begin
                            row_w0 <= input_data;
                            if (key_change) begin
                                // close the live group; this row opens the next
                                // once its w1 arrives after the emit
                                held_is_pending <= 1'b1;
                                final_after_emit <= 1'b0;
                                state <= STATE_EMIT_R0;
                            end else begin
                                state <= STATE_ROW_HIGH;
                            end
                        end
                    end
                end
                STATE_ROW_HIGH, STATE_PENDING_HIGH: begin
                    if (input_fire) begin
                        if ((state == STATE_PENDING_HIGH) || !grp_open) begin
                            open_group(row_lanes);
                        end else begin
                            absorb_row(row_lanes[1*FIELD_BITS +: DATA_BITS]);
                        end
                        if (final_row) begin
                            final_after_emit <= 1'b1;
                            state <= STATE_EMIT_R0;
                        end else begin
                            state <= STATE_ROW_LOW;
                        end
                    end
                end
                STATE_EMIT_R0: if (!output_valid || output_ready) emit_word({32'd0, SLOT_BITS'(signed'(grp_key))}, 1'b0, STATE_EMIT_R1);
                STATE_EMIT_R1: if (!output_valid || output_ready) emit_word(stat_pair(f_last[0], f_first[0]), 1'b0, STATE_EMIT_R2);
                STATE_EMIT_R2: if (!output_valid || output_ready) emit_word(stat_pair(f_max[0], f_min[0]), 1'b0, STATE_EMIT_R3);
                STATE_EMIT_R3: if (!output_valid || output_ready) emit_word(unsigned'(f_sum[0]), 1'b0, STATE_EMIT_R4);
                STATE_EMIT_R4: if (!output_valid || output_ready) emit_word(stat_pair(f_last[1], f_first[1]), 1'b0, STATE_EMIT_R5);
                STATE_EMIT_R5: if (!output_valid || output_ready) emit_word(stat_pair(f_max[1], f_min[1]), 1'b0, STATE_EMIT_R6);
                STATE_EMIT_R6: if (!output_valid || output_ready) emit_word(unsigned'(f_sum[1]), 1'b0, STATE_EMIT_R7);
                STATE_EMIT_R7: if (!output_valid || output_ready) emit_word(stat_pair(f_last[2], f_first[2]), 1'b0, STATE_EMIT_R8);
                STATE_EMIT_R8: if (!output_valid || output_ready) emit_word(stat_pair(f_max[2], f_min[2]), 1'b0, STATE_EMIT_R9);
                STATE_EMIT_R9: if (!output_valid || output_ready) emit_word(unsigned'(f_sum[2]), 1'b0, STATE_EMIT_R10);
                STATE_EMIT_R10: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= grp_rows;
                        output_last <= final_after_emit && !held_is_pending;
                        group_count <= group_count + 64'd1;
                        grp_open <= 1'b0;
                        if (held_is_pending) begin
                            held_is_pending <= 1'b0;
                            state <= STATE_PENDING_HIGH;
                        end else begin
                            state <= STATE_STATUS;
                        end
                    end
                end
                STATE_DRAIN_ERROR: begin
                    if (input_fire && input_last) begin
                        start_error_status(ERR_STREAM);
                    end
                end
                STATE_STATUS: begin
                    if (!status_valid) begin
                        if (!output_valid || output_ready) begin
                            status_valid <= 1'b1;
                            status_error <= 1'b0;
                            status_error_code <= 8'd0;
                        end
                    end else if (status_fire) begin
                        reset_stream_state();
                    end
                end
                default: reset_state();
            endcase
        end
    end

    // the record's element slot: one statistic, sign-extended into 32 bits,
    // so an 11-word record decodes the same way at every element type
    function automatic logic [63:0] stat_pair(input logic signed [FIELD_BITS-1:0] hi, input logic signed [FIELD_BITS-1:0] lo);
        stat_pair = {SLOT_BITS'(signed'(hi)), SLOT_BITS'(signed'(lo))};
    endfunction

    task automatic open_group(input logic [LANE_BITS-1:0] lanes);
        logic signed [FIELD_BITS-1:0] vals [DATA_FIELDS];
        begin
            grp_open <= 1'b1;
            grp_key <= signed'(lanes[0*FIELD_BITS +: FIELD_BITS]);
            vals[0] = signed'(lanes[1*FIELD_BITS +: FIELD_BITS]);
            vals[1] = signed'(lanes[2*FIELD_BITS +: FIELD_BITS]);
            vals[2] = signed'(lanes[3*FIELD_BITS +: FIELD_BITS]);
            for (int i = 0; i < DATA_FIELDS; i++) begin
                f_first[i] <= vals[i];
                f_last[i] <= vals[i];
                f_min[i] <= vals[i];
                f_max[i] <= vals[i];
                f_sum[i] <= 64'(vals[i]);
            end
            grp_rows <= 64'd1;
        end
    endtask

    task automatic absorb_row(input logic [DATA_BITS-1:0] data_lanes);
        logic signed [FIELD_BITS-1:0] vals [DATA_FIELDS];
        begin
            vals[0] = signed'(data_lanes[0*FIELD_BITS +: FIELD_BITS]);
            vals[1] = signed'(data_lanes[1*FIELD_BITS +: FIELD_BITS]);
            vals[2] = signed'(data_lanes[2*FIELD_BITS +: FIELD_BITS]);
            for (int i = 0; i < DATA_FIELDS; i++) begin
                f_last[i] <= vals[i];
                if (vals[i] < f_min[i]) f_min[i] <= vals[i];
                if (vals[i] > f_max[i]) f_max[i] <= vals[i];
                f_sum[i] <= f_sum[i] + 64'(vals[i]);
            end
            grp_rows <= grp_rows + 64'd1;
        end
    endtask

    task automatic emit_word(input logic [63:0] value, input logic last_word, input state_t next_state);
        begin
            output_valid <= 1'b1;
            output_data <= value;
            output_last <= last_word;
            state <= next_state;
        end
    endtask

    task automatic start_error_status(input logic [7:0] code);
        begin
            output_valid <= 1'b0;
            output_last <= 1'b0;
            held_is_pending <= 1'b0;
            grp_open <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_stream_state;
        begin
            state <= STATE_ROW_LOW;
            grp_open <= 1'b0;
            final_after_emit <= 1'b0;
            held_is_pending <= 1'b0;
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
            grp_key <= '0;
            grp_rows <= 64'd0;
            row_w0 <= 64'd0;
            for (int i = 0; i < DATA_FIELDS; i++) begin
                f_first[i] <= '0;
                f_last[i] <= '0;
                f_min[i] <= '0;
                f_max[i] <= '0;
                f_sum[i] <= 64'sd0;
            end
        end
    endtask
endmodule

`default_nettype wire

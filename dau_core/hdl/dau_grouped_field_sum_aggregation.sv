`default_nettype none

`include "types.svh"

// Ordered grouped field-sum aggregation for 16-byte quad rows (two 64-bit
// words, w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th
// FIELD_WIDTH lane of the row, which at the default int32 is
// (key, value, aux0, aux1) order). Consecutive rows with equal
// signed keys (field 0, the row's first lane) form a group — the sort-then-stream
// contract: the host stages the scan pre-sorted/pre-bucketed on the group
// key, so group state is one live group, bounded and known. A group closes
// when the key changes or the batch ends, emitting one record: the group
// key sign-extended to a 64-bit word, one 64-bit word per
// cfg_field_mask-selected field (two's-complement INT64 sum, ascending
// field index), then the group's row-count word
// (dau_core.operators.grouped_field_sum_rows is the golden). q1's
// per-group multi-sum is a mask of the same record; derived expressions
// (disc_price, charge) arrive chain-computed by row-map-alu stages.
//
// The batch's group records form ONE record stream: output_last is set
// only on the final group's count word, and the tile raises exactly one
// success close-out per batch after that word is accepted (the terminal
// tile convention — the record writer's done waits on a consumed producer
// status). group_count reports the groups emitted by the last completed
// batch (0 after an error close-out). A key that reappears after other
// keys opens a NEW group; partials merge on the host, never in the tile.
//
// The mask is latched at the first row of a batch; a zero mask is an
// unconfigured tile and drains the batch to report ERR_CONFIG. input_last
// on a w0 is a torn row and reports ERR_STREAM. An error close-out
// invalidates the whole batch: group records already drained before the
// error (earlier groups close as their key changes) must be discarded by
// the consumer — group_count reads 0 and the record writer's done never
// sees a success close-out for the batch.
module dau_grouped_field_sum_aggregation #(
    // The ELEMENT TYPE of one field, as encoded by types.svh. The 128-bit row
    // is the transport unit and stays fixed; field i is its i-th FIELD_BITS
    // lane, the decode row-unpack packs and the quad-row filter family reads.
    // This tile groups on lane 0 and sums the FIELDS lanes it names, ignoring
    // any a narrower type leaves beyond them -- the lane count is what the
    // accumulators cost. `DAU_TYPE_INT32 is 32, so the default reproduces the
    // four-field row port for port. The sums are INT64 and the emitted key
    // word is 64 bits whatever the element is, which is why the type may
    // narrow but not widen.
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
    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;

    localparam int ROW_BITS = 128;   // the transport unit, fixed
    localparam int FIELDS   = 4;     // the lanes this tile sums
    localparam int FIELD_BITS      = `DAU_TYPE_WIDTH(FIELD_WIDTH);
    localparam int FIELD_IS_FLOAT  = `DAU_TYPE_IS_FLOAT(FIELD_WIDTH);
    localparam int FIELD_IS_SIGNED = `DAU_TYPE_IS_SIGNED(FIELD_WIDTH);
    localparam int LANE_BITS       = FIELDS * FIELD_BITS;  // the part of the row this tile reads

    // The upper bound is the RECORD, not the lane: a sum word is 64 bits, so
    // an element wider than 32 would need more sum bits than the record can
    // express -- a versioned record, i.e. a different tile. Narrowing is free.
    if (FIELD_BITS < 8 || FIELD_BITS > 32 || (ROW_BITS % FIELD_BITS) != 0) begin : g_field_width_check
        $error("FIELD_WIDTH must divide 128 and lie in [8, 32]");
    end
    // A TYPE IS NOT A CAPABILITY. This datapath sign-extends each field into a
    // two's-complement INT64 accumulator and compares keys for equality after
    // the same extension: handed a float it would add exponent bits as though
    // they were magnitude, and handed an unsigned type it would read every
    // high-bit-set value as negative. Both are silently wrong sums.
    if (FIELD_IS_FLOAT != 0) begin : g_field_float_check
        $error("FIELD_WIDTH must not name a floating-point type: this tile sums two's-complement integers");
    end
    if (FIELD_IS_SIGNED == 0) begin : g_field_signed_check
        $error("FIELD_WIDTH must not name an unsigned type: this tile sign-extends into a signed accumulator");
    end

    typedef enum logic [2:0] {
        STATE_W0,      // accept w0 of a row (batch config latches on the first)
        STATE_W1,      // accept w1 of a row, open/absorb the live group
        STATE_EMIT,    // walk key word, selected sums, then the count word
        STATE_CLOSE,   // final count word accepted -> raise the close-out
        STATE_DISCARD, // bad config: consume the batch to input_last
        STATE_STATUS   // hold a close-out (success or error) until accepted
    } state_t;

    state_t state;

    logic        batch_open;
    logic [3:0]  active_mask;
    logic [63:0] row_w0;
    logic        group_open;
    logic signed [FIELD_BITS-1:0] group_key;
    logic signed [63:0] sums [FIELDS];
    logic [63:0] group_rows;
    logic [63:0] batch_groups;
    logic [2:0]  emit_phase;  // 0 = key word, 1..4 walk the fields, 5 = count
    logic        emit_final;  // this record closes the batch's record stream

    logic input_fire;
    logic output_fire;
    logic first_row;
    logic signed [FIELD_BITS-1:0] row_key;
    // the row being completed: w0 was registered, w1 is on the input, and the
    // lanes are read out of the assembled row rather than out of either word,
    // because at a narrower type a lane no longer lines up with a word. At
    // int32 the kept lanes are the whole row and the cast is the identity.
    logic [LANE_BITS-1:0] row_lanes;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;

    assign input_ready = (state == STATE_W0) || (state == STATE_W1) || (state == STATE_DISCARD);

    assign first_row = !batch_open;
    // in STATE_W0 the incoming word is w0, whose low lane is the group key
    assign row_key = signed'(input_data[0*FIELD_BITS +: FIELD_BITS]);
    assign row_lanes = LANE_BITS'({input_data, row_w0});

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_W0: begin
                    if (input_fire) begin
                        if (first_row) begin
                            batch_open <= 1'b1;
                            active_mask <= cfg_field_mask;
                            batch_groups <= 64'd0;
                        end
                        if (first_row && (cfg_field_mask == 4'd0)) begin
                            // unconfigured: drain the batch, then CONFIG
                            if (input_last) begin
                                raise_error(ERR_CONFIG);
                            end else begin
                                state <= STATE_DISCARD;
                            end
                        end else if (input_last) begin
                            // torn row: the batch is invalid
                            raise_error(ERR_STREAM);
                        end else begin
                            row_w0 <= input_data;
                            if (group_open && (row_key != group_key)) begin
                                // close the live group; the held w0 opens
                                // the next group at STATE_W1 after the emit
                                emit_phase <= 3'd0;
                                emit_final <= 1'b0;
                                state <= STATE_EMIT;
                            end else begin
                                state <= STATE_W1;
                            end
                        end
                    end
                end
                STATE_W1: begin
                    if (input_fire) begin
                        if (!group_open) begin
                            open_group(row_lanes);
                        end else begin
                            absorb_row(row_lanes);
                        end
                        if (input_last) begin
                            emit_phase <= 3'd0;
                            emit_final <= 1'b1;
                            state <= STATE_EMIT;
                        end else begin
                            state <= STATE_W0;
                        end
                    end
                end
                STATE_EMIT: begin
                    if (!output_valid || output_ready) begin
                        if (emit_phase == 3'd0) begin
                            output_valid <= 1'b1;
                            output_data <= 64'(signed'(group_key));
                            output_last <= 1'b0;
                            emit_phase <= 3'd1;
                        end else if (emit_phase < 3'd5) begin
                            if (active_mask[emit_phase[1:0] - 2'd1]) begin
                                output_valid <= 1'b1;
                                output_data <= unsigned'(sums[emit_phase[1:0] - 2'd1]);
                                output_last <= 1'b0;
                            end
                            emit_phase <= emit_phase + 3'd1;
                        end else begin
                            // the count word closes the group record
                            output_valid <= 1'b1;
                            output_data <= group_rows;
                            output_last <= emit_final;
                            batch_groups <= batch_groups + 64'd1;
                            group_open <= 1'b0;
                            if (emit_final) begin
                                state <= STATE_CLOSE;
                            end else begin
                                // the held w0 opens the next group
                                state <= STATE_W1;
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
                        raise_error(ERR_CONFIG);
                    end
                end
                STATE_STATUS: begin
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= STATE_W0;
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    task automatic open_group(input logic [LANE_BITS-1:0] lanes);
        begin
            group_open <= 1'b1;
            group_key <= signed'(lanes[0*FIELD_BITS +: FIELD_BITS]);
            sums[0] <= active_mask[0] ? 64'(signed'(lanes[0*FIELD_BITS +: FIELD_BITS])) : 64'sd0;
            sums[1] <= active_mask[1] ? 64'(signed'(lanes[1*FIELD_BITS +: FIELD_BITS])) : 64'sd0;
            sums[2] <= active_mask[2] ? 64'(signed'(lanes[2*FIELD_BITS +: FIELD_BITS])) : 64'sd0;
            sums[3] <= active_mask[3] ? 64'(signed'(lanes[3*FIELD_BITS +: FIELD_BITS])) : 64'sd0;
            group_rows <= 64'd1;
        end
    endtask

    task automatic absorb_row(input logic [LANE_BITS-1:0] lanes);
        begin
            if (active_mask[0]) begin
                sums[0] <= sums[0] + 64'(signed'(lanes[0*FIELD_BITS +: FIELD_BITS]));
            end
            if (active_mask[1]) begin
                sums[1] <= sums[1] + 64'(signed'(lanes[1*FIELD_BITS +: FIELD_BITS]));
            end
            if (active_mask[2]) begin
                sums[2] <= sums[2] + 64'(signed'(lanes[2*FIELD_BITS +: FIELD_BITS]));
            end
            if (active_mask[3]) begin
                sums[3] <= sums[3] + 64'(signed'(lanes[3*FIELD_BITS +: FIELD_BITS]));
            end
            group_rows <= group_rows + 64'd1;
        end
    endtask

    task automatic raise_error(input logic [7:0] code);
        begin
            output_valid <= 1'b0;
            output_last <= 1'b0;
            batch_open <= 1'b0;
            group_open <= 1'b0;
            group_count <= 64'd0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_W0;
            batch_open <= 1'b0;
            active_mask <= 4'd0;
            row_w0 <= 64'd0;
            group_open <= 1'b0;
            group_key <= '0;
            for (int i = 0; i < FIELDS; i++) begin
                sums[i] <= 64'sd0;
            end
            group_rows <= 64'd0;
            batch_groups <= 64'd0;
            emit_phase <= 3'd0;
            emit_final <= 1'b0;
            output_valid <= 1'b0;
            output_data <= 64'd0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            group_count <= 64'd0;
        end
    endtask
endmodule

`default_nettype wire

`default_nettype none

`include "types.svh"

// Batch field-sum aggregation for 16-byte quad rows (two 64-bit words,
// w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th FIELD_WIDTH lane
// of the row, which at the default int32 is (key, value, aux0, aux1) order).
// One pass accumulates a signed 64-bit sum per cfg_field_mask-selected field
// plus the row count, and at input_last emits exactly one record: one 64-bit
// word per selected field (two's-complement sum, ascending field index), then
// the count word with output_last set (dau_core.operators.field_sum_rows is
// the golden). q6's SUM(revenue) is a one-bit mask; q1's multi-sum shape is a
// wider mask of the same core, not a new tile. Then a success close-out
// status (the tile convention: the record writer's done waits on a consumed
// producer status); aggregated_count reports the rows accumulated by the last
// completed batch (0 after an error close-out).
//
// The mask is latched at the first row of a batch; a zero mask is an
// unconfigured tile and drains the batch to report ERR_CONFIG. input_last
// on a w0 is a torn row and reports ERR_STREAM.
module dau_field_sum_aggregation #(
    // The ELEMENT TYPE of one field, as encoded by types.svh. The 128-bit row
    // is the transport unit and stays fixed, and field i is its i-th
    // FIELD_BITS lane -- the same decode row-unpack packs and the quad-row
    // filter family reads. This tile aggregates the FIELDS lanes it names and
    // ignores any a narrower type leaves beyond them: the lane count is what
    // the accumulators cost, so it is a resource decision rather than
    // something the element type should silently multiply.
    //
    // A plain signed integer encodes as its own width, so `DAU_TYPE_INT32 is
    // 32 and this reproduces the four-field row the tile has always had, port
    // for port. The sums are INT64 whatever the element is, which is why the
    // type may narrow but not widen (see the guard).
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
    output logic [63:0]      aggregated_count
);
    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;

    localparam int ROW_BITS = 128;   // the transport unit, fixed
    localparam int FIELDS   = 4;     // the lanes this tile accumulates
    // the type, taken apart: the lanes size off FIELD_BITS, and the remaining
    // facts are what the guards check the type against
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
    // two's-complement INT64 accumulator: handed a float it would add exponent
    // bits as though they were magnitude, and handed an unsigned type it would
    // read every high-bit-set value as negative. Both are silently wrong sums.
    if (FIELD_IS_FLOAT != 0) begin : g_field_float_check
        $error("FIELD_WIDTH must not name a floating-point type: this tile sums two's-complement integers");
    end
    if (FIELD_IS_SIGNED == 0) begin : g_field_signed_check
        $error("FIELD_WIDTH must not name an unsigned type: this tile sign-extends into a signed accumulator");
    end

    typedef enum logic [2:0] {
        STATE_W0,      // accept w0 of a row (batch config latches on the first)
        STATE_W1,      // accept w1 of a row, accumulate
        STATE_EMIT,    // walk the selected sums, then the count word
        STATE_CLOSE,   // count word accepted -> raise the success close-out
        STATE_DISCARD, // bad config: consume the batch to input_last
        STATE_STATUS   // hold a close-out (success or error) until accepted
    } state_t;

    state_t state;

    logic        batch_open;
    logic [3:0]  active_mask;
    logic [63:0] row_w0;
    logic signed [63:0] sums [FIELDS];
    logic [63:0] row_count;
    logic [2:0]  emit_phase;  // 0..3 walk the fields, 4 emits the count

    logic input_fire;
    logic output_fire;
    logic first_row;
    logic [3:0] eff_mask;
    // the row being completed: w0 was registered, w1 is on the input, and the
    // lanes are read out of the assembled row rather than out of either word,
    // because at a narrower type a lane no longer lines up with a word. Only
    // the FIELDS lanes this tile accumulates are kept; at int32 they are the
    // whole row and the cast is the identity.
    logic [LANE_BITS-1:0] row_lanes;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;

    assign input_ready = (state == STATE_W0) || (state == STATE_W1) || (state == STATE_DISCARD);

    assign first_row = !batch_open;
    assign eff_mask = first_row ? cfg_field_mask : active_mask;
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
                            for (int i = 0; i < FIELDS; i++) begin
                                sums[i] <= 64'sd0;
                            end
                            row_count <= 64'd0;
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
                            state <= STATE_W1;
                        end
                    end
                end
                STATE_W1: begin
                    if (input_fire) begin
                        if (eff_mask[0]) begin
                            sums[0] <= sums[0] + 64'(signed'(row_lanes[0*FIELD_BITS +: FIELD_BITS]));
                        end
                        if (eff_mask[1]) begin
                            sums[1] <= sums[1] + 64'(signed'(row_lanes[1*FIELD_BITS +: FIELD_BITS]));
                        end
                        if (eff_mask[2]) begin
                            sums[2] <= sums[2] + 64'(signed'(row_lanes[2*FIELD_BITS +: FIELD_BITS]));
                        end
                        if (eff_mask[3]) begin
                            sums[3] <= sums[3] + 64'(signed'(row_lanes[3*FIELD_BITS +: FIELD_BITS]));
                        end
                        row_count <= row_count + 64'd1;
                        if (input_last) begin
                            emit_phase <= 3'd0;
                            state <= STATE_EMIT;
                        end else begin
                            state <= STATE_W0;
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
                            // the count word closes the record
                            output_valid <= 1'b1;
                            output_data <= row_count;
                            output_last <= 1'b1;
                            state <= STATE_CLOSE;
                        end
                    end
                end
                STATE_CLOSE: begin
                    // success close-out only after the count word is accepted
                    // (the record writer's done waits on a consumed status)
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
                        state <= STATE_W0;
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
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_W0;
            batch_open <= 1'b0;
            active_mask <= 4'd0;
            row_w0 <= 64'd0;
            for (int i = 0; i < FIELDS; i++) begin
                sums[i] <= 64'sd0;
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
        end
    endtask
endmodule

`default_nettype wire

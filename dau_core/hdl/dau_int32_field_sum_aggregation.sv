`default_nettype none

// Batch field-sum aggregation for 16-byte quad rows (two 64-bit words,
// w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th 32-bit lane in
// (key, value, aux0, aux1) order). One pass accumulates a signed 64-bit
// sum per cfg_field_mask-selected field plus the row count, and at
// input_last emits exactly one record: one 64-bit word per selected field
// (two's-complement sum, ascending field index), then the count word with
// output_last set (dau_core.operators.field_sum_rows is the golden). q6's
// SUM(revenue) is a one-bit mask; q1's multi-sum shape is a wider mask of
// the same core, not a new tile. Then a success close-out status (the tile
// convention: the record writer's done waits on a consumed producer
// status); aggregated_count reports the rows accumulated by the last
// completed batch (0 after an error close-out).
//
// The mask is latched at the first row of a batch; a zero mask is an
// unconfigured tile and drains the batch to report ERR_CONFIG. input_last
// on a w0 is a torn row and reports ERR_STREAM.
module dau_int32_field_sum_aggregation (
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
    logic signed [63:0] sums [4];
    logic [63:0] row_count;
    logic [2:0]  emit_phase;  // 0..3 walk the fields, 4 emits the count

    logic input_fire;
    logic output_fire;
    logic first_row;
    logic [3:0] eff_mask;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;

    assign input_ready = (state == STATE_W0) || (state == STATE_W1) || (state == STATE_DISCARD);

    assign first_row = !batch_open;
    assign eff_mask = first_row ? cfg_field_mask : active_mask;

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
                            for (int i = 0; i < 4; i++) begin
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
                            sums[0] <= sums[0] + 64'(signed'(row_w0[31:0]));
                        end
                        if (eff_mask[1]) begin
                            sums[1] <= sums[1] + 64'(signed'(row_w0[63:32]));
                        end
                        if (eff_mask[2]) begin
                            sums[2] <= sums[2] + 64'(signed'(input_data[31:0]));
                        end
                        if (eff_mask[3]) begin
                            sums[3] <= sums[3] + 64'(signed'(input_data[63:32]));
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
            for (int i = 0; i < 4; i++) begin
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

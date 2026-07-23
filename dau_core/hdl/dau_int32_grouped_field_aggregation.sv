`default_nettype none

// Generic multi-field grouped-aggregation tile (ordered close-out): over quad
// rows (key, v0, v1, v2) grouped by consecutive-equal key, computes for EACH of
// the three data fields its first / last / min / max / sum (INT64) in one pass,
// plus the row count. Workload-agnostic — OHLCV bars are a field/stat SELECTION
// over this record (open/high/low/close = field0 first/max/min/last, volume =
// sum field1, ...), not a baked-in tile; the finance column assignment lives in
// the host / a downstream select. Matches
// dau_core.operators.grouped_field_aggregate_rows bit-for-bit.
//
// Two 64-bit words per row (a row is atomic; input_last only legal on w1):
//   w0 = {v0[31:0], key[31:0]}
//   w1 = {v2[31:0], v1[31:0]}
// A group closes when the key changes or the stream ends; a reappearing key
// opens a NEW partial group (the host merges — the partial-group contract).
// Close-out emits an 11-word record:
//   r0  = {32'd0, key}
//   r1  = {f0_last, f0_first}     r2  = {f0_max, f0_min}     r3  = f0_sum[63:0]
//   r4  = {f1_last, f1_first}     r5  = {f1_max, f1_min}     r6  = f1_sum[63:0]
//   r7  = {f2_last, f2_first}     r8  = {f2_max, f2_min}     r9  = f2_sum[63:0]
//   r10 = count[63:0]
// A row torn by input_last on w0 is ERR_STREAM.
module dau_int32_grouped_field_aggregation (
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
    logic signed [31:0] grp_key;
    // per field: first / last / min / max / sum(INT64)
    logic signed [31:0] f_first [3];
    logic signed [31:0] f_last  [3];
    logic signed [31:0] f_min   [3];
    logic signed [31:0] f_max   [3];
    logic signed [63:0] f_sum   [3];
    logic [63:0] grp_rows;

    logic        final_after_emit;
    logic signed [31:0] held_key;
    logic signed [31:0] held_v0;
    logic        held_is_pending;

    logic input_fire;
    logic output_fire;
    logic status_fire;
    logic final_row;
    logic key_change;

    logic signed [31:0] row_key;
    logic signed [31:0] row_v0;
    logic signed [31:0] row_v1;
    logic signed [31:0] row_v2;

    assign input_ready = ((state == STATE_ROW_LOW) || (state == STATE_ROW_HIGH)
        || (state == STATE_PENDING_HIGH) || (state == STATE_DRAIN_ERROR))
        && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    assign final_row = input_last;
    assign row_key = signed'(input_data[31:0]);
    assign row_v0 = signed'(input_data[63:32]);
    assign row_v1 = signed'(input_data[31:0]);
    assign row_v2 = signed'(input_data[63:32]);
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
                            held_key <= row_key;
                            held_v0 <= row_v0;
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
                            open_group(held_key, held_v0, row_v1, row_v2);
                        end else begin
                            absorb_row(held_v0, row_v1, row_v2);
                        end
                        if (final_row) begin
                            final_after_emit <= 1'b1;
                            state <= STATE_EMIT_R0;
                        end else begin
                            state <= STATE_ROW_LOW;
                        end
                    end
                end
                STATE_EMIT_R0: if (!output_valid || output_ready) emit_word({32'd0, grp_key}, 1'b0, STATE_EMIT_R1);
                STATE_EMIT_R1: if (!output_valid || output_ready) emit_word({f_last[0], f_first[0]}, 1'b0, STATE_EMIT_R2);
                STATE_EMIT_R2: if (!output_valid || output_ready) emit_word({f_max[0], f_min[0]}, 1'b0, STATE_EMIT_R3);
                STATE_EMIT_R3: if (!output_valid || output_ready) emit_word(unsigned'(f_sum[0]), 1'b0, STATE_EMIT_R4);
                STATE_EMIT_R4: if (!output_valid || output_ready) emit_word({f_last[1], f_first[1]}, 1'b0, STATE_EMIT_R5);
                STATE_EMIT_R5: if (!output_valid || output_ready) emit_word({f_max[1], f_min[1]}, 1'b0, STATE_EMIT_R6);
                STATE_EMIT_R6: if (!output_valid || output_ready) emit_word(unsigned'(f_sum[1]), 1'b0, STATE_EMIT_R7);
                STATE_EMIT_R7: if (!output_valid || output_ready) emit_word({f_last[2], f_first[2]}, 1'b0, STATE_EMIT_R8);
                STATE_EMIT_R8: if (!output_valid || output_ready) emit_word({f_max[2], f_min[2]}, 1'b0, STATE_EMIT_R9);
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

    task automatic open_group(
        input logic signed [31:0] key,
        input logic signed [31:0] v0,
        input logic signed [31:0] v1,
        input logic signed [31:0] v2
    );
        logic signed [31:0] vals [3];
        begin
            grp_open <= 1'b1;
            grp_key <= key;
            vals[0] = v0;
            vals[1] = v1;
            vals[2] = v2;
            for (int i = 0; i < 3; i++) begin
                f_first[i] <= vals[i];
                f_last[i] <= vals[i];
                f_min[i] <= vals[i];
                f_max[i] <= vals[i];
                f_sum[i] <= 64'(vals[i]);
            end
            grp_rows <= 64'd1;
        end
    endtask

    task automatic absorb_row(
        input logic signed [31:0] v0,
        input logic signed [31:0] v1,
        input logic signed [31:0] v2
    );
        logic signed [31:0] vals [3];
        begin
            vals[0] = v0;
            vals[1] = v1;
            vals[2] = v2;
            for (int i = 0; i < 3; i++) begin
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
            grp_key <= 32'sd0;
            grp_rows <= 64'd0;
            held_key <= 32'sd0;
            held_v0 <= 32'sd0;
            for (int i = 0; i < 3; i++) begin
                f_first[i] <= 32'sd0;
                f_last[i] <= 32'sd0;
                f_min[i] <= 32'sd0;
                f_max[i] <= 32'sd0;
                f_sum[i] <= 64'sd0;
            end
        end
    endtask
endmodule

`default_nettype wire

`default_nettype none

module dau_int32_arrow_lite_stream_aggregation (
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
    output logic [7:0]       status_error_code
);
    localparam logic [31:0] DAU_MAGIC = 32'h44415531;
    localparam logic [7:0]  VERSION_MAJOR = 8'd0;
    localparam logic [7:0]  VERSION_MINOR = 8'd1;
    localparam logic [7:0]  HEADER_LENGTH_BYTES = 8'd64;

    localparam logic [15:0] LT_INT32  = 16'd12;
    localparam logic [15:0] LT_INT64  = 16'd13;
    localparam logic [15:0] LT_UINT64 = 16'd23;

    localparam logic [2:0] OP_SUM   = 3'd3;
    localparam logic [2:0] OP_COUNT = 3'd4;

    localparam logic [7:0] ERR_DESCRIPTOR = 8'd1;
    localparam logic [7:0] ERR_OPCODE     = 8'd2;
    localparam logic [7:0] ERR_TILE       = 8'd3;

    localparam logic [63:0] DESCRIPTOR_TABLE_OFFSET = 64'd64;
    localparam logic [63:0] INPUT_PAYLOAD_OFFSET = 64'd192;
    localparam logic [63:0] RESULT_DESCRIPTOR_OFFSET = 64'd64;
    localparam logic [63:0] RESULT_PAYLOAD_OFFSET = 64'd128;
    localparam logic [4:0]  RESULT_LAST_WORD = 5'd16;

    typedef enum logic [3:0] {
        STATE_HEADER,
        STATE_COLUMN,
        STATE_OPERATION,
        STATE_PAYLOAD,
        STATE_DRAIN_SUCCESS,
        STATE_DRAIN_ERROR,
        STATE_OUTPUT,
        STATE_STATUS
    } state_t;

    state_t state;

    logic [63:0] word_index;
    logic [63:0] schema_hash;
    logic [63:0] batch_id;
    logic [63:0] active_row_count;
    logic [63:0] payload_length;
    logic [2:0]  active_opcode;
    logic [63:0] seen_count;
    logic [63:0] rows_left;
    logic        payload_phase;      // 1 = held high half still to process
    logic [31:0] held_high;
    logic        held_last;
    logic signed [63:0] accumulator;
    logic signed [63:0] result_value;
    logic [15:0] result_logical_type;
    logic [4:0]  output_word_index;
    logic [7:0]  pending_error_code;

    logic input_fire;
    logic output_fire;
    logic status_fire;
    logic [63:0] expected_payload_length;
    logic [31:0] current_sample;
    logic signed [63:0] next_accumulator;

    // One sample per cycle keeps the 64-bit compare/accumulate chain inside
    // the 8 ns budget on the -2 Artix (two chained stages missed timing).
    assign input_ready = (state != STATE_OUTPUT) && (state != STATE_STATUS) && !output_valid && !status_valid
        && !((state == STATE_PAYLOAD) && payload_phase);
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;
    assign expected_payload_length = active_row_count << 2;
    assign current_sample = payload_phase ? held_high : input_data[31:0];
    assign next_accumulator = dau_aggregation_pkg::aggregate_next(active_opcode, accumulator, seen_count, $signed(current_sample), seen_count + 64'd1);
    assign output_last = output_valid && (output_word_index == RESULT_LAST_WORD);

    always_comb begin
        unique case (output_word_index)
            5'd0: output_data = {8'd0, HEADER_LENGTH_BYTES, VERSION_MINOR, VERSION_MAJOR, DAU_MAGIC};
            5'd1: output_data = schema_hash;
            5'd2: output_data = batch_id;
            5'd3: output_data = 64'd1;
            5'd4: output_data = {16'd0, 16'd0, 16'd0, 16'd1};
            5'd5: output_data = RESULT_DESCRIPTOR_OFFSET;
            5'd6: output_data = RESULT_PAYLOAD_OFFSET;
            5'd7: output_data = 64'd8;
            5'd8: output_data = {16'd0, 16'd64, result_logical_type, 16'd0};
            5'd9: output_data = 64'd1;
            5'd10: output_data = 64'd0;
            5'd11: output_data = RESULT_PAYLOAD_OFFSET;
            5'd12: output_data = 64'd8;
            5'd13: output_data = 64'd0;
            5'd14: output_data = 64'd0;
            5'd15: output_data = {32'd0, 16'd0, 16'd2};
            5'd16: output_data = result_value;
            default: output_data = 64'd0;
        endcase
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (state == STATE_OUTPUT) begin
                if (output_fire) begin
                    if (output_word_index == RESULT_LAST_WORD) begin
                        output_valid <= 1'b0;
                        output_word_index <= 5'd0;
                        status_valid <= 1'b1;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= STATE_STATUS;
                    end else begin
                        output_word_index <= output_word_index + 5'd1;
                    end
                end
            end else if (state == STATE_STATUS) begin
                if (status_fire) begin
                    reset_state();
                end
            end else if ((state == STATE_PAYLOAD) && payload_phase) begin
                handle_high_sample();
            end else if (input_fire) begin
                word_index <= word_index + 64'd1;
                unique case (state)
                    STATE_HEADER: handle_header_word();
                    STATE_COLUMN: handle_column_word();
                    STATE_OPERATION: handle_operation_word();
                    STATE_PAYLOAD: handle_payload_word();
                    STATE_DRAIN_SUCCESS: begin
                        if (input_last) begin
                            start_output();
                        end
                    end
                    STATE_DRAIN_ERROR: begin
                        if (input_last) begin
                            start_error_status(pending_error_code);
                        end
                    end
                    default: begin
                        start_error_status(ERR_TILE);
                    end
                endcase
            end
        end
    end

    task automatic reset_state;
        begin
            state <= STATE_HEADER;
            word_index <= 64'd0;
            schema_hash <= 64'd0;
            batch_id <= 64'd0;
            active_row_count <= 64'd0;
            payload_length <= 64'd0;
            active_opcode <= OP_SUM;
            seen_count <= 64'd0;
            rows_left <= 64'd0;
            payload_phase <= 1'b0;
            held_high <= 32'd0;
            held_last <= 1'b0;
            accumulator <= 64'sd0;
            result_value <= 64'sd0;
            result_logical_type <= LT_INT64;
            output_word_index <= 5'd0;
            output_valid <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            pending_error_code <= 8'd0;
        end
    endtask

    task automatic handle_header_word;
        begin
            unique case (word_index)
                64'd0: begin
                    if ((input_data[31:0] != DAU_MAGIC)
                            || (input_data[39:32] != VERSION_MAJOR)
                            || (input_data[47:40] != VERSION_MINOR)
                            || (input_data[55:48] != HEADER_LENGTH_BYTES)) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd1: schema_hash <= input_data;
                64'd2: batch_id <= input_data;
                64'd3: active_row_count <= input_data;
                64'd4: begin
                    if ((input_data[15:0] != 16'd1) || (input_data[31:16] != 16'd1) || (input_data[47:32] != 16'd0)) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd5: begin
                    if (input_data != DESCRIPTOR_TABLE_OFFSET) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd6: begin
                    if (input_data != INPUT_PAYLOAD_OFFSET) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd7: begin
                    payload_length <= input_data;
                    if ((active_row_count == 64'd0) || (input_data != expected_payload_length)) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end else begin
                        state <= STATE_COLUMN;
                    end
                end
                default: drain_or_report_error(ERR_DESCRIPTOR);
            endcase
        end
    endtask

    task automatic handle_column_word;
        begin
            unique case (word_index)
                64'd8: begin
                    if ((input_data[31:16] != LT_INT32) || (input_data[47:32] != 16'd32)) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd9: begin
                    if (input_data != active_row_count) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd10: begin
                    if (input_data != 64'd0) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd11: begin
                    if (input_data != INPUT_PAYLOAD_OFFSET) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd12: begin
                    if (input_data != payload_length) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd13, 64'd14: begin
                    if (input_data != 64'd0) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd15: begin
                    if (input_data[15:0] != 16'd0) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end else begin
                        state <= STATE_OPERATION;
                    end
                end
                default: drain_or_report_error(ERR_DESCRIPTOR);
            endcase
        end
    endtask

    task automatic handle_operation_word;
        begin
            unique case (word_index)
                64'd16: begin
                    active_opcode <= input_data[18:16];
                    if ((input_data[31:16] != 16'd1)
                            && (input_data[31:16] != 16'd2)
                            && (input_data[31:16] != 16'd3)
                            && (input_data[31:16] != 16'd4)) begin
                        drain_or_report_error(ERR_OPCODE);
                    end else if (input_data[47:32] != 16'd0) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd17: begin
                    if (input_data[31:16] != 16'd64) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end else if (((active_opcode == OP_COUNT) && (input_data[15:0] != LT_UINT64))
                            || ((active_opcode != OP_COUNT) && (input_data[15:0] != LT_INT64))) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd18: begin
                    if (input_data[15:0] != 16'd0) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd19, 64'd20, 64'd21, 64'd22: begin
                end
                64'd23: begin
                    rows_left <= active_row_count;
                    payload_phase <= 1'b0;
                    state <= STATE_PAYLOAD;
                end
                default: drain_or_report_error(ERR_DESCRIPTOR);
            endcase
        end
    endtask

    // phase 0: process the low sample of a freshly accepted payload word
    task automatic handle_payload_word;
        begin
            accumulator <= next_accumulator;
            seen_count <= seen_count + 64'd1;
            rows_left <= rows_left - 64'd1;
            if (rows_left == 64'd1) begin
                finish_payload(next_accumulator, input_last);
            end else begin
                held_high <= input_data[63:32];
                held_last <= input_last;
                payload_phase <= 1'b1;
            end
        end
    endtask

    // phase 1: process the held high sample (no new input accepted)
    task automatic handle_high_sample;
        begin
            accumulator <= next_accumulator;
            seen_count <= seen_count + 64'd1;
            rows_left <= rows_left - 64'd1;
            payload_phase <= 1'b0;
            if (rows_left == 64'd1) begin
                finish_payload(next_accumulator, held_last);
            end else if (held_last) begin
                start_error_status(ERR_DESCRIPTOR);
            end
        end
    endtask

    task automatic finish_payload(input logic signed [63:0] final_value, input logic was_last_word);
        begin
            result_value <= final_value;
            result_logical_type <= (active_opcode == OP_COUNT) ? LT_UINT64 : LT_INT64;
            seen_count <= 64'd0;
            accumulator <= 64'sd0;
            payload_phase <= 1'b0;
            if (was_last_word) begin
                start_output();
            end else begin
                state <= STATE_DRAIN_SUCCESS;
            end
        end
    endtask

    task automatic drain_or_report_error(input logic [7:0] code);
        begin
            pending_error_code <= code;
            if (input_last) begin
                start_error_status(code);
            end else begin
                state <= STATE_DRAIN_ERROR;
            end
        end
    endtask

    task automatic start_output;
        begin
            state <= STATE_OUTPUT;
            word_index <= 64'd0;
            output_valid <= 1'b1;
            output_word_index <= 5'd0;
        end
    endtask

    task automatic start_error_status(input logic [7:0] code);
        begin
            state <= STATE_STATUS;
            word_index <= 64'd0;
            payload_phase <= 1'b0;
            output_valid <= 1'b0;
            output_word_index <= 5'd0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
        end
    endtask

endmodule

`default_nettype wire

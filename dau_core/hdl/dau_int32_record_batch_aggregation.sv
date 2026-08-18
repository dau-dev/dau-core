`default_nettype none

`include "types.svh"

// Multi-column record-batch scalar aggregation.
//
// Consumes a packed 64-bit DAU record-batch stream carrying up to
// MAX_COLUMNS fixed-width, null-free columns plus exactly one operation
// descriptor, aggregates the column selected by the operation's
// input_column_id, and emits the standard 17-word scalar result stream. The
// selected column must be of the element type VALUE_WIDTH names; the
// accumulator and the result descriptor are INT64 whatever that element is,
// which is why the type may narrow but not widen.
module dau_int32_record_batch_aggregation #(
    parameter int unsigned MAX_COLUMNS = 16,
    // The ELEMENT TYPE of the aggregated column, as encoded by types.svh.
    // `DAU_TYPE_INT32 is 32, so the default selects exactly the INT32 columns
    // the tile has always selected, two samples to a payload word.
    parameter int VALUE_WIDTH = `DAU_TYPE_INT32
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
    output logic [7:0]       status_error_code
);
    localparam logic [31:0] DAU_MAGIC = 32'h44415531;
    localparam logic [7:0]  VERSION_MAJOR = 8'd0;
    localparam logic [7:0]  VERSION_MINOR = 8'd1;
    localparam logic [7:0]  HEADER_LENGTH_BYTES = 8'd64;

    localparam int VALUE_BITS      = `DAU_TYPE_WIDTH(VALUE_WIDTH);
    localparam int VALUE_IS_FLOAT  = `DAU_TYPE_IS_FLOAT(VALUE_WIDTH);
    localparam int VALUE_IS_SIGNED = `DAU_TYPE_IS_SIGNED(VALUE_WIDTH);
    localparam int VALUE_BYTES     = VALUE_BITS / 8;
    // the payload packs this many samples into each 64-bit word; the walk
    // below takes the first straight off the accepted word and holds the rest
    localparam int SAMPLES_PER_WORD = 64 / VALUE_BITS;
    localparam int PHASE_BITS = $clog2(SAMPLES_PER_WORD);
    localparam int HELD_BITS  = 64 - VALUE_BITS;
    localparam logic [PHASE_BITS-1:0] PHASE_LAST = PHASE_BITS'(SAMPLES_PER_WORD - 1);

    // arrow-lite logical type ids run 10, 11, 12, 13 for int8/16/32/64 (and
    // 20..23 unsigned), so a signed integer's id is 10 + log2(bytes). This is
    // the ONE place the element type meets the descriptor protocol.
    localparam logic [15:0] LT_ELEMENT = 16'(10 + $clog2(VALUE_BYTES));
    localparam logic [15:0] LT_INT64  = 16'd13;
    localparam logic [15:0] LT_UINT64 = 16'd23;

    // The upper bound is the RESULT, not the lane: the accumulator and the
    // result descriptor are INT64, so a wider element could overflow a sum the
    // result cannot express -- a versioned result, i.e. a different tile.
    if (VALUE_BITS < 8 || VALUE_BITS > 32 || (64 % VALUE_BITS) != 0) begin : g_value_width_check
        $error("VALUE_WIDTH must divide 64 and lie in [8, 32]");
    end
    // A TYPE IS NOT A CAPABILITY. MIN/MAX order the sample and SUM adds it
    // into a two's-complement INT64: a float would be ordered by its exponent
    // bits, an unsigned type read as negative above its half-range.
    if (VALUE_IS_FLOAT != 0) begin : g_value_float_check
        $error("VALUE_WIDTH must not name a floating-point type: this tile orders and sums two's-complement integers");
    end
    if (VALUE_IS_SIGNED == 0) begin : g_value_signed_check
        $error("VALUE_WIDTH must not name an unsigned type: this tile orders and sums two's-complement integers");
    end

    localparam logic [2:0] OP_SUM   = 3'd3;
    localparam logic [2:0] OP_COUNT = 3'd4;

    localparam logic [7:0] ERR_DESCRIPTOR = 8'd1;
    localparam logic [7:0] ERR_OPCODE     = 8'd2;
    localparam logic [7:0] ERR_TILE       = 8'd3;

    localparam logic [63:0] DESCRIPTOR_TABLE_OFFSET = 64'd64;
    localparam logic [63:0] RESULT_DESCRIPTOR_OFFSET = 64'd64;
    localparam logic [63:0] RESULT_PAYLOAD_OFFSET = 64'd128;
    localparam logic [4:0]  RESULT_LAST_WORD = 5'd16;

    typedef enum logic [3:0] {
        STATE_HEADER,
        STATE_COLUMN,
        STATE_OPERATION,
        STATE_SKIP,
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
    logic [15:0] column_count;
    logic [63:0] header_payload_offset;
    logic [15:0] column_index;
    logic [2:0]  descriptor_word;
    logic [63:0] column_payload_offset [MAX_COLUMNS];
    logic [63:0] column_payload_length [MAX_COLUMNS];
    logic        column_is_element [MAX_COLUMNS];
    logic [2:0]  active_opcode;
    logic [15:0] selected_column;
    logic [63:0] payload_start_word;
    logic [63:0] seen_count;
    logic [63:0] rows_left;
    logic [PHASE_BITS-1:0] payload_phase;  // 0 = take the next sample off a fresh word
    logic [HELD_BITS-1:0]  held_rest;      // the word's remaining samples, right-aligned
    logic        held_last;
    logic signed [63:0] accumulator;
    logic signed [63:0] result_value;
    logic [15:0] result_logical_type;
    logic [4:0]  output_word_index;
    logic [7:0]  pending_error_code;

    logic input_fire;
    logic output_fire;
    logic status_fire;
    logic [VALUE_BITS-1:0] current_sample;
    logic        payload_pending;
    logic signed [63:0] next_accumulator;

    // One sample per cycle keeps the 64-bit compare/accumulate chain inside
    // the 8 ns budget on the -2 Artix (two chained stages missed timing).
    assign payload_pending = (payload_phase != '0);
    assign input_ready = (state != STATE_OUTPUT) && (state != STATE_STATUS) && !output_valid && !status_valid
        && !((state == STATE_PAYLOAD) && payload_pending);
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;
    assign current_sample = payload_pending ? held_rest[VALUE_BITS-1:0] : input_data[VALUE_BITS-1:0];
    assign next_accumulator = dau_aggregation_pkg::aggregate_next(active_opcode, accumulator, seen_count, 64'(signed'(current_sample)), seen_count + 64'd1);
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
            end else if ((state == STATE_PAYLOAD) && payload_pending) begin
                handle_held_sample();
            end else if (input_fire) begin
                word_index <= word_index + 64'd1;
                unique case (state)
                    STATE_HEADER: handle_header_word();
                    STATE_COLUMN: handle_column_word();
                    STATE_OPERATION: handle_operation_word();
                    STATE_SKIP: handle_skip_word();
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
            column_count <= 16'd0;
            header_payload_offset <= 64'd0;
            column_index <= 16'd0;
            descriptor_word <= 3'd0;
            active_opcode <= OP_SUM;
            selected_column <= 16'd0;
            payload_start_word <= 64'd0;
            seen_count <= 64'd0;
            rows_left <= 64'd0;
            payload_phase <= '0;
            held_rest <= '0;
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
                    column_count <= input_data[15:0];
                    if ((input_data[15:0] == 16'd0)
                            || (input_data[15:0] > MAX_COLUMNS[15:0])
                            || (input_data[31:16] != 16'd1)
                            || (input_data[47:32] != 16'd0)) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd5: begin
                    if (input_data != DESCRIPTOR_TABLE_OFFSET) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd6: begin
                    header_payload_offset <= input_data;
                    // header + N column descriptors + 1 operation descriptor
                    if (input_data != (64'd64 * (64'd2 + {48'd0, column_count}))) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                64'd7: begin
                    if (active_row_count == 64'd0) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end else begin
                        state <= STATE_COLUMN;
                        column_index <= 16'd0;
                        descriptor_word <= 3'd0;
                    end
                end
                default: drain_or_report_error(ERR_DESCRIPTOR);
            endcase
        end
    endtask

    task automatic handle_column_word;
        begin
            descriptor_word <= descriptor_word + 3'd1;
            unique case (descriptor_word)
                3'd0: begin
                    column_is_element[column_index[$clog2(MAX_COLUMNS)-1:0]] <=
                        (input_data[31:16] == LT_ELEMENT) && (input_data[47:32] == 16'(VALUE_BITS));
                end
                3'd1: begin
                    if (input_data != active_row_count) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                3'd2: begin
                    if (input_data != 64'd0) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                3'd3: column_payload_offset[column_index[$clog2(MAX_COLUMNS)-1:0]] <= input_data;
                3'd4: column_payload_length[column_index[$clog2(MAX_COLUMNS)-1:0]] <= input_data;
                3'd5, 3'd6: begin
                    if (input_data != 64'd0) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                3'd7: begin
                    if (input_data[15:0] != 16'd0) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end else if (column_index + 16'd1 == column_count) begin
                        state <= STATE_OPERATION;
                        descriptor_word <= 3'd0;
                    end else begin
                        column_index <= column_index + 16'd1;
                        descriptor_word <= 3'd0;
                    end
                end
                default: drain_or_report_error(ERR_DESCRIPTOR);
            endcase
        end
    endtask

    task automatic handle_operation_word;
        begin
            descriptor_word <= descriptor_word + 3'd1;
            unique case (descriptor_word)
                3'd0: begin
                    active_opcode <= input_data[18:16];
                    selected_column <= input_data[47:32];
                    if ((input_data[31:16] != 16'd1)
                            && (input_data[31:16] != 16'd2)
                            && (input_data[31:16] != 16'd3)
                            && (input_data[31:16] != 16'd4)) begin
                        drain_or_report_error(ERR_OPCODE);
                    end else if (input_data[47:32] >= column_count) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                3'd1: begin
                    if (input_data[31:16] != 16'd64) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end else if (((active_opcode == OP_COUNT) && (input_data[15:0] != LT_UINT64))
                            || ((active_opcode != OP_COUNT) && (input_data[15:0] != LT_INT64))) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                3'd2: begin
                    if (input_data[15:0] != 16'd0) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end
                end
                3'd3, 3'd4, 3'd5, 3'd6: begin
                end
                3'd7: begin
                    if (!column_is_element[selected_column[$clog2(MAX_COLUMNS)-1:0]]
                            || (column_payload_length[selected_column[$clog2(MAX_COLUMNS)-1:0]] != (active_row_count << $clog2(VALUE_BYTES)))
                            || (column_payload_offset[selected_column[$clog2(MAX_COLUMNS)-1:0]][5:0] != 6'd0)) begin
                        drain_or_report_error(ERR_DESCRIPTOR);
                    end else begin
                        payload_start_word <= column_payload_offset[selected_column[$clog2(MAX_COLUMNS)-1:0]] >> 3;
                        rows_left <= active_row_count;
                        payload_phase <= '0;
                        if ((column_payload_offset[selected_column[$clog2(MAX_COLUMNS)-1:0]] >> 3) == (word_index + 64'd1)) begin
                            state <= STATE_PAYLOAD;
                        end else begin
                            state <= STATE_SKIP;
                        end
                    end
                end
                default: drain_or_report_error(ERR_DESCRIPTOR);
            endcase
        end
    endtask

    task automatic handle_skip_word;
        begin
            if (input_last) begin
                // stream ended before the selected column's payload
                start_error_status(ERR_DESCRIPTOR);
            end else if ((word_index + 64'd1) == payload_start_word) begin
                state <= STATE_PAYLOAD;
            end
        end
    endtask

    // phase 0: process the first sample of a freshly accepted payload word and
    // hold the rest of it
    task automatic handle_payload_word;
        begin
            accumulator <= next_accumulator;
            seen_count <= seen_count + 64'd1;
            rows_left <= rows_left - 64'd1;
            if (rows_left == 64'd1) begin
                // final sample; the rest of the word is alignment padding
                finish_payload(next_accumulator, input_last);
            end else begin
                held_rest <= input_data[63:VALUE_BITS];
                held_last <= input_last;
                payload_phase <= PHASE_BITS'(1);
            end
        end
    endtask

    // phases 1..SAMPLES_PER_WORD-1: process a held sample (no new input
    // accepted). At the last held sample the word is spent and the walk
    // rearms; below it the remaining samples shift down.
    task automatic handle_held_sample;
        begin
            accumulator <= next_accumulator;
            seen_count <= seen_count + 64'd1;
            rows_left <= rows_left - 64'd1;
            if (rows_left == 64'd1) begin
                finish_payload(next_accumulator, held_last);
            end else if (payload_phase == PHASE_LAST) begin
                payload_phase <= '0;
                if (held_last) begin
                    // truncated payload: stream ended with rows outstanding
                    start_error_status(ERR_DESCRIPTOR);
                end
            end else begin
                payload_phase <= payload_phase + PHASE_BITS'(1);
                held_rest <= HELD_BITS'(held_rest >> VALUE_BITS);
            end
        end
    endtask

    task automatic finish_payload(input logic signed [63:0] final_value, input logic was_last_word);
        begin
            result_value <= final_value;
            result_logical_type <= (active_opcode == OP_COUNT) ? LT_UINT64 : LT_INT64;
            seen_count <= 64'd0;
            accumulator <= 64'sd0;
            payload_phase <= '0;
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
            payload_phase <= '0;
            output_valid <= 1'b0;
            output_word_index <= 5'd0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
        end
    endtask

endmodule

`default_nettype wire

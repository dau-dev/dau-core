`default_nettype none

`include "types.svh"

// Descriptor-driven scalar aggregation over one fixed-width column: the
// descriptor names the column's arrow-lite logical type and physical width,
// and this tile accepts exactly the one its VALUE_WIDTH parameter names. The
// accumulator is INT64 and the result descriptor says INT64/UINT64 whatever
// the input element is, which is why the type may narrow but not widen.
module dau_stream_aggregation #(
    // The ELEMENT TYPE of the aggregated column, as encoded by types.svh.
    // `DAU_TYPE_INT32 is 32, so the default accepts exactly the INT32 column
    // the tile has always accepted, descriptor field for descriptor field.
    parameter int VALUE_WIDTH = `DAU_TYPE_INT32
) (
    input  wire logic              clk,
    input  wire logic              rst,
    input  wire logic              descriptor_valid,
    output logic                   descriptor_ready,
    input  wire logic [63:0]       descriptor_row_count,
    input  wire logic [15:0]       descriptor_logical_type,
    input  wire logic [15:0]       descriptor_physical_width_bits,
    input  wire logic [63:0]       descriptor_payload_length,
    input  wire logic [2:0]        descriptor_opcode,
    input  wire logic              input_valid,
    output logic                   input_ready,
    input  wire logic signed [VALUE_BITS-1:0] input_value,
    output logic                   result_valid,
    input  wire logic              result_ready,
    output logic signed [63:0]     result_value,
    output logic [15:0]            result_logical_type,
    output logic [63:0]            result_payload_length,
    output logic [63:0]            result_row_count,
    output logic                   error,
    output logic [7:0]             error_code
);
    localparam int VALUE_BITS      = `DAU_TYPE_WIDTH(VALUE_WIDTH);
    localparam int VALUE_IS_FLOAT  = `DAU_TYPE_IS_FLOAT(VALUE_WIDTH);
    localparam int VALUE_IS_SIGNED = `DAU_TYPE_IS_SIGNED(VALUE_WIDTH);
    localparam int VALUE_BYTES     = VALUE_BITS / 8;

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

    localparam logic [2:0] OP_MIN   = 3'd1;
    localparam logic [2:0] OP_MAX   = 3'd2;
    localparam logic [2:0] OP_SUM   = 3'd3;
    localparam logic [2:0] OP_COUNT = 3'd4;

    localparam logic [7:0] ERR_DESCRIPTOR = 8'd1;
    localparam logic [7:0] ERR_OPCODE     = 8'd2;

    typedef enum logic [0:0] {
        STATE_IDLE,
        STATE_RUNNING
    } state_t;

    state_t state;

    logic [2:0]        active_opcode;
    logic [63:0]       active_row_count;
    logic [63:0]       accepted_count;
    logic [63:0]       next_accepted_count;
    logic [63:0]       expected_payload_length;
    logic              descriptor_fields_valid;
    logic              descriptor_opcode_supported;
    logic              descriptor_is_valid;
    logic              input_fire;
    logic              input_last;
    logic signed [63:0] accumulator;
    logic signed [63:0] next_accumulator;

    assign expected_payload_length = descriptor_row_count << $clog2(VALUE_BYTES);
    assign descriptor_fields_valid = (descriptor_logical_type == LT_ELEMENT)
        && (descriptor_physical_width_bits == 16'(VALUE_BITS))
        && (descriptor_row_count != 64'd0)
        && (descriptor_payload_length == expected_payload_length);
    assign descriptor_opcode_supported = (descriptor_opcode == OP_MIN)
        || (descriptor_opcode == OP_MAX)
        || (descriptor_opcode == OP_SUM)
        || (descriptor_opcode == OP_COUNT);
    assign descriptor_is_valid = descriptor_fields_valid && descriptor_opcode_supported;

    assign descriptor_ready = (state == STATE_IDLE) && !result_valid;
    assign input_ready = (state == STATE_RUNNING) && !result_valid;
    assign input_fire = input_valid && input_ready;
    assign next_accepted_count = accepted_count + 64'd1;
    assign input_last = (next_accepted_count == active_row_count);
    assign next_accumulator = dau_aggregation_pkg::aggregate_next(
        active_opcode, accumulator, accepted_count, 64'(input_value), next_accepted_count
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= STATE_IDLE;
            active_opcode <= OP_SUM;
            active_row_count <= 64'd0;
            accepted_count <= 64'd0;
            accumulator <= 64'sd0;
            result_valid <= 1'b0;
            result_value <= 64'sd0;
            result_logical_type <= 16'd0;
            result_payload_length <= 64'd0;
            result_row_count <= 64'd0;
            error <= 1'b0;
            error_code <= 8'd0;
        end else if (result_valid) begin
            if (result_ready) begin
                result_valid <= 1'b0;
                error <= 1'b0;
                error_code <= 8'd0;
            end
        end else if (state == STATE_IDLE) begin
            if (descriptor_valid && descriptor_ready) begin
                if (descriptor_is_valid) begin
                    state <= STATE_RUNNING;
                    active_opcode <= descriptor_opcode;
                    active_row_count <= descriptor_row_count;
                    accepted_count <= 64'd0;
                    accumulator <= 64'sd0;
                    error <= 1'b0;
                    error_code <= 8'd0;
                end else begin
                    state <= STATE_IDLE;
                    active_opcode <= OP_SUM;
                    active_row_count <= 64'd0;
                    accepted_count <= 64'd0;
                    accumulator <= 64'sd0;
                    result_valid <= 1'b1;
                    result_value <= 64'sd0;
                    result_logical_type <= 16'd0;
                    result_payload_length <= 64'd0;
                    result_row_count <= 64'd0;
                    error <= 1'b1;
                    error_code <= descriptor_fields_valid ? ERR_OPCODE : ERR_DESCRIPTOR;
                end
            end
        end else if (input_fire) begin
            if (input_last) begin
                state <= STATE_IDLE;
                accepted_count <= 64'd0;
                accumulator <= 64'sd0;
                result_valid <= 1'b1;
                result_value <= next_accumulator;
                result_logical_type <= (active_opcode == OP_COUNT) ? LT_UINT64 : LT_INT64;
                result_payload_length <= 64'd8;
                result_row_count <= 64'd1;
                error <= 1'b0;
                error_code <= 8'd0;
            end else begin
                accepted_count <= next_accepted_count;
                accumulator <= next_accumulator;
            end
        end
    end
endmodule

`default_nettype wire

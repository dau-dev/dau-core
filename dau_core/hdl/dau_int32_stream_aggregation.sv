`default_nettype none

module dau_int32_stream_aggregation (
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
    input  wire logic signed [31:0] input_value,
    output logic                   result_valid,
    input  wire logic              result_ready,
    output logic signed [63:0]     result_value,
    output logic [15:0]            result_logical_type,
    output logic [63:0]            result_payload_length,
    output logic [63:0]            result_row_count,
    output logic                   error,
    output logic [7:0]             error_code
);
    localparam logic [15:0] LT_INT32  = 16'd12;
    localparam logic [15:0] LT_INT64  = 16'd13;
    localparam logic [15:0] LT_UINT64 = 16'd23;

    localparam logic [2:0] OP_MIN   = 3'd1;
    localparam logic [2:0] OP_MAX   = 3'd2;
    localparam logic [2:0] OP_SUM   = 3'd3;
    localparam logic [2:0] OP_COUNT = 3'd4;

    localparam logic [7:0] ERR_DESCRIPTOR = 8'd1;
    localparam logic [7:0] ERR_OPCODE     = 8'd2;
    localparam logic [7:0] ERR_TILE       = 8'd3;

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
    logic              tile_input_ready;
    logic              tile_result_valid;
    logic signed [63:0] tile_result_value;
    logic              tile_error;

    assign expected_payload_length = descriptor_row_count << 2;
    assign descriptor_fields_valid = (descriptor_logical_type == LT_INT32)
        && (descriptor_physical_width_bits == 16'd32)
        && (descriptor_row_count != 64'd0)
        && (descriptor_payload_length == expected_payload_length);
    assign descriptor_opcode_supported = (descriptor_opcode == OP_MIN)
        || (descriptor_opcode == OP_MAX)
        || (descriptor_opcode == OP_SUM)
        || (descriptor_opcode == OP_COUNT);
    assign descriptor_is_valid = descriptor_fields_valid && descriptor_opcode_supported;

    assign descriptor_ready = (state == STATE_IDLE) && !result_valid;
    assign input_ready = (state == STATE_RUNNING) && tile_input_ready && !result_valid;
    assign input_fire = input_valid && input_ready;
    assign next_accepted_count = accepted_count + 64'd1;
    assign input_last = (next_accepted_count == active_row_count);

    dau_int32_aggregation_tile tile (
        .clk(clk),
        .rst(rst),
        .input_valid(input_fire),
        .input_ready(tile_input_ready),
        .input_last(input_last),
        .input_value(input_value),
        .input_opcode(active_opcode),
        .result_valid(tile_result_valid),
        .result_ready(result_valid && result_ready),
        .result_value(tile_result_value),
        .error(tile_error)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= STATE_IDLE;
            active_opcode <= OP_SUM;
            active_row_count <= 64'd0;
            accepted_count <= 64'd0;
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
                    error <= 1'b0;
                    error_code <= 8'd0;
                end else begin
                    state <= STATE_IDLE;
                    active_opcode <= OP_SUM;
                    active_row_count <= 64'd0;
                    accepted_count <= 64'd0;
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
            end else begin
                accepted_count <= next_accepted_count;
            end
        end

        if (!rst && !result_valid && tile_result_valid) begin
            result_valid <= 1'b1;
            result_value <= tile_result_value;
            result_logical_type <= (active_opcode == OP_COUNT) ? LT_UINT64 : LT_INT64;
            result_payload_length <= 64'd8;
            result_row_count <= 64'd1;
            error <= tile_error;
            error_code <= tile_error ? ERR_TILE : 8'd0;
        end
    end
endmodule

`default_nettype wire
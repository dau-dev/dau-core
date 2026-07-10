`default_nettype none

module dau_int32_aggregation_tile (
    input  wire logic              clk,
    input  wire logic              rst,
    input  wire logic              input_valid,
    output logic                   input_ready,
    input  wire logic              input_last,
    input  wire logic signed [31:0] input_value,
    input  wire logic [2:0]        input_opcode,
    output logic                   result_valid,
    input  wire logic              result_ready,
    output logic signed [63:0]     result_value,
    output logic                   error
);
    localparam logic [2:0] OP_MIN   = 3'd1;
    localparam logic [2:0] OP_MAX   = 3'd2;
    localparam logic [2:0] OP_SUM   = 3'd3;
    localparam logic [2:0] OP_COUNT = 3'd4;

    logic signed [63:0] accumulator;
    logic signed [63:0] next_accumulator;
    logic [63:0]        sample_count;
    logic [63:0]        next_sample_count;

    assign input_ready = !result_valid;
    assign next_sample_count = sample_count + 64'd1;
    assign next_accumulator = dau_aggregation_pkg::aggregate_next(input_opcode, accumulator, sample_count, input_value, next_sample_count);

    always_ff @(posedge clk) begin
        if (rst) begin
            accumulator <= 64'sd0;
            sample_count <= 64'd0;
            result_valid <= 1'b0;
            result_value <= 64'sd0;
            error <= 1'b0;
        end else if (result_valid) begin
            if (result_ready) begin
                result_valid <= 1'b0;
                error <= 1'b0;
            end
        end else if (input_valid && input_ready) begin
            unique case (input_opcode)
                OP_MIN, OP_MAX, OP_SUM, OP_COUNT: begin
                    error <= 1'b0;
                    if (input_last) begin
                        accumulator <= 64'sd0;
                        sample_count <= 64'd0;
                        result_value <= next_accumulator;
                        result_valid <= 1'b1;
                    end else begin
                        accumulator <= next_accumulator;
                        sample_count <= next_sample_count;
                        result_valid <= 1'b0;
                    end
                end
                default: begin
                    accumulator <= 64'sd0;
                    sample_count <= 64'd0;
                    result_value <= 64'sd0;
                    result_valid <= 1'b1;
                    error <= 1'b1;
                end
            endcase
        end
    end
endmodule

`default_nettype wire

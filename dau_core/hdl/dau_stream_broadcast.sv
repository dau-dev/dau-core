`default_nettype none

// N-way stream broadcast: every output must accept every beat (taken-bit
// bookkeeping, so a slow consumer stalls the beat only for consumers that
// have not yet taken it). Data and last fan out combinationally; the input
// beat completes when the last straggler accepts.
module dau_stream_broadcast #(
    parameter int unsigned NUM_OUTPUTS = 2
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        input_valid,
    output logic             input_ready,
    input  wire logic [63:0] input_data,
    input  wire logic        input_last,
    output logic [NUM_OUTPUTS-1:0] output_valid,
    input  wire logic [NUM_OUTPUTS-1:0] output_ready,
    output logic [63:0]      output_data,
    output logic             output_last
);
    logic [NUM_OUTPUTS-1:0] taken;
    logic [NUM_OUTPUTS-1:0] accepting;

    assign output_data = input_data;
    assign output_last = input_last;
    assign output_valid = {NUM_OUTPUTS{input_valid}} & ~taken;
    assign accepting = output_valid & output_ready;
    assign input_ready = &(taken | accepting);

    always_ff @(posedge clk) begin
        if (rst) begin
            taken <= '0;
        end else if (input_valid && input_ready) begin
            taken <= '0;
        end else begin
            taken <= taken | accepting;
        end
    end
endmodule

`default_nettype wire

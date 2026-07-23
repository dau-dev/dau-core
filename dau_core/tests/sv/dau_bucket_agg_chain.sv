`default_nettype none

// Fused bar stage, all generic: the time-bucket key rewrite streams straight
// into the generic multi-field aggregator. Rewritten rows never touch DDR
// between the two tiles, and the aggregator's per-field first/last/min/max/sum
// record is selected into OHLCV bars downstream (dau_core.operators.
// bars_from_field_aggregate) — no finance-specific bar tile. The bucket tile is
// silent on success, so its output_last simply rides into the aggregator, which
// closes the chain out; the bucket's (error-only) status is drained.
module dau_bucket_agg_chain #(
    parameter int BUCKET_BITS = 16
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [5:0]  cfg_bucket_shift,
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
    // the bucketed row stream feeding the aggregator
    logic        keyed_valid;
    logic        keyed_ready;
    logic [63:0] keyed_data;
    logic        keyed_last;

    // the bucket tile's error-only status (silent on success), drained here
    logic       bucket_status_valid;
    logic       bucket_status_error;
    logic [7:0] bucket_status_error_code;

    dau_int32_time_bucket_key #(.BUCKET_BITS(BUCKET_BITS)) u_bucket (
        .clk(clk),
        .rst(rst),
        .cfg_bucket_shift(cfg_bucket_shift),
        .input_valid(input_valid),
        .input_ready(input_ready),
        .input_data(input_data),
        .input_last(input_last),
        .output_valid(keyed_valid),
        .output_ready(keyed_ready),
        .output_data(keyed_data),
        .output_last(keyed_last),
        .status_valid(bucket_status_valid),
        .status_ready(1'b1),  // silent on success; drain the error-only status
        .status_error(bucket_status_error),
        .status_error_code(bucket_status_error_code)
    );

    dau_int32_grouped_field_aggregation u_agg (
        .clk(clk),
        .rst(rst),
        .input_valid(keyed_valid),
        .input_ready(keyed_ready),
        .input_data(keyed_data),
        .input_last(keyed_last),
        .output_valid(output_valid),
        .output_ready(output_ready),
        .output_data(output_data),
        .output_last(output_last),
        .status_valid(status_valid),
        .status_ready(status_ready),
        .status_error(status_error),
        .status_error_code(status_error_code),
        .group_count(group_count)
    );
endmodule

`default_nettype wire

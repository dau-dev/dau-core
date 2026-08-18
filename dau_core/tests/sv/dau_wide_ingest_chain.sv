`default_nettype none

module dau_wide_ingest_chain (
    input  wire logic          clk,
    input  wire logic          rst,
    input  wire logic [31:0]   cfg_input_records,
    input  wire logic          input_valid,
    output logic               input_ready,
    input  wire logic [511:0]  input_data,
    input  wire logic          input_last,
    output logic               record_valid,
    output logic               record_ready,
    output logic [7:0]         output_valid,
    input  wire logic [7:0]    output_ready,
    output logic [511:0]       output_data,
    output logic [7:0]         output_last,
    output logic [7:0]         status_valid,
    input  wire logic [7:0]    status_ready,
    output logic [7:0]         status_error,
    output logic [63:0]        status_error_code,
    output logic               gearbox_status_valid,
    input  wire logic          gearbox_status_ready,
    output logic               gearbox_status_error,
    output logic [7:0]         gearbox_status_error_code
);
    logic [191:0] record_data;
    logic         record_last;

    dau_record_gearbox #(
        .IN_WIDTH(512),
        .RECORD_WORDS(3)
    ) u_gearbox (
        .clk(clk),
        .rst(rst),
        .cfg_input_records(cfg_input_records),
        .input_valid(input_valid),
        .input_ready(input_ready),
        .input_data(input_data),
        .input_last(input_last),
        .output_valid(record_valid),
        .output_ready(record_ready),
        .output_data(record_data),
        .output_last(record_last),
        .status_valid(gearbox_status_valid),
        .status_ready(gearbox_status_ready),
        .status_error(gearbox_status_error),
        .status_error_code(gearbox_status_error_code)
    );

    dau_key_mask_dispatcher #(
        .NUM_PARTITIONS(8),
        .IN_WIDTH(192),
        .RECORD_WORDS(3),
        .RECORD_INPUT(1'b1)
    ) u_dispatcher (
        .clk(clk),
        .rst(rst),
        .input_valid(record_valid),
        .input_ready(record_ready),
        .input_data(record_data),
        .input_last(record_last),
        .output_valid(output_valid),
        .output_ready(output_ready),
        .output_data(output_data),
        .output_last(output_last),
        .status_valid(status_valid),
        .status_ready(status_ready),
        .status_error(status_error),
        .status_error_code(status_error_code)
    );
endmodule

`default_nettype wire

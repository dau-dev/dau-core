`default_nettype none

module dau_identity_registers #(
    // register map 0.3: OPERATOR_BITMAP is the hardware truth of the
    // composed design; LANE_COUNT / HOST_OPCODE_BITMAP / SORT_CAPACITY
    // advertise the composition, and INPUT_ROW_CAPACITY /
    // OUTPUT_RECORD_CAPACITY advertise how much ONE job may stage
    // (0 = not advertised).
    parameter logic [31:0] REGISTER_MAP_VERSION = 32'h0000_0003,
    parameter logic [31:0] STREAM_PROTOCOL_VERSION = 32'h0000_0001,
    parameter logic [31:0] PLATFORM_ID = 32'h3156_5044,
    parameter logic [63:0] BUILD_ID = 64'h0000_0000_0000_0000,
    parameter logic [31:0] STATUS = 32'h0000_0000,
    parameter logic [31:0] OPERATOR_BITMAP = 32'h0000_0000,
    parameter logic [31:0] LANE_COUNT = 32'h0000_0000,
    parameter logic [31:0] HOST_OPCODE_BITMAP = 32'h0000_0000,
    parameter logic [31:0] SORT_CAPACITY = 32'h0000_0000,
    // the shell's BRAM depths, in rows and records: what the host must batch
    // to. Left at zero the host has no capacity to work from and refuses,
    // which is the point -- it used to fall back on constants compiled into
    // the driver and batch one shell against another shell's depths.
    parameter logic [31:0] INPUT_ROW_CAPACITY = 32'h0000_0000,
    parameter logic [31:0] OUTPUT_RECORD_CAPACITY = 32'h0000_0000,
    parameter logic [31:0] LAST_ERROR = 32'h0000_0000,
    parameter logic [63:0] H2C_BYTES = 64'h0000_0000_0000_0000,
    parameter logic [63:0] C2H_BYTES = 64'h0000_0000_0000_0000,
    parameter logic [63:0] ROWS = 64'h0000_0000_0000_0000,
    parameter logic [31:0] BATCHES = 32'h0000_0000,
    parameter logic [31:0] STALLS = 32'h0000_0000
) (
    input  wire logic [15:0] addr,
    input  wire logic        wen,
    input  wire logic [31:0] wdata,
    output logic [31:0] reset_request,
    output logic [31:0] rdata
);
    assign reset_request = (wen && (addr == 16'h0020)) ? wdata : 32'h0000_0000;

    assign rdata =
        (addr == 16'h0000) ? 32'h4441_5531 :
        (addr == 16'h0004) ? REGISTER_MAP_VERSION :
        (addr == 16'h0008) ? STREAM_PROTOCOL_VERSION :
        (addr == 16'h000c) ? PLATFORM_ID :
        (addr == 16'h0014) ? BUILD_ID[31:0] :
        (addr == 16'h0018) ? BUILD_ID[63:32] :
        (addr == 16'h001c) ? STATUS :
        (addr == 16'h0020) ? 32'h0000_0000 :
        (addr == 16'h0028) ? OPERATOR_BITMAP :
        (addr == 16'h002c) ? LAST_ERROR :
        (addr == 16'h0030) ? H2C_BYTES[31:0] :
        (addr == 16'h0034) ? H2C_BYTES[63:32] :
        (addr == 16'h0038) ? C2H_BYTES[31:0] :
        (addr == 16'h003c) ? C2H_BYTES[63:32] :
        (addr == 16'h0040) ? ROWS[31:0] :
        (addr == 16'h0044) ? ROWS[63:32] :
        (addr == 16'h0048) ? BATCHES :
        (addr == 16'h004c) ? STALLS :
        (addr == 16'h00c0) ? LANE_COUNT :
        (addr == 16'h00c4) ? HOST_OPCODE_BITMAP :
        (addr == 16'h00c8) ? SORT_CAPACITY :
        (addr == 16'h00cc) ? INPUT_ROW_CAPACITY :
        (addr == 16'h00d0) ? OUTPUT_RECORD_CAPACITY :
        32'hffff_ffff;
endmodule

`default_nettype wire

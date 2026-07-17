`default_nettype none

module dau_identity_axil #(
    parameter [31:0] REGISTER_MAP_VERSION = 32'h0000_0002,
    parameter [31:0] STREAM_PROTOCOL_VERSION = 32'h0000_0001,
    parameter [31:0] PLATFORM_ID = 32'h4554_494e,
    parameter [31:0] SHELL_VERSION = 32'h0000_0000,
    parameter [63:0] BUILD_ID = 64'h0000_0000_0000_0000,
    parameter [31:0] STATUS = 32'h0000_0000,
    parameter [31:0] DMA_CONSTRAINTS = 32'h0000_0000,
    parameter [31:0] OPERATOR_BITMAP = 32'h0000_0000,
    parameter [31:0] LANE_COUNT = 32'h0000_0000,
    parameter [31:0] HOST_OPCODE_BITMAP = 32'h0000_0000,
    parameter [31:0] SORT_CAPACITY = 32'h0000_0000,
    parameter [31:0] LAST_ERROR = 32'h0000_0000,
    parameter [63:0] H2C_BYTES = 64'h0000_0000_0000_0000,
    parameter [63:0] C2H_BYTES = 64'h0000_0000_0000_0000,
    parameter [63:0] ROWS = 64'h0000_0000_0000_0000,
    parameter [31:0] BATCHES = 32'h0000_0000,
    parameter [31:0] STALLS = 32'h0000_0000
) (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 s_axi_aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "ASSOCIATED_BUSIF S_AXI, ASSOCIATED_RESET s_axi_aresetn" *)
    input wire s_axi_aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 s_axi_aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "POLARITY ACTIVE_LOW" *)
    input wire s_axi_aresetn,

    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWADDR" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME S_AXI, PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 16, HAS_BURST 0, HAS_LOCK 0, HAS_PROT 0, HAS_CACHE 0, HAS_QOS 0, HAS_REGION 0, HAS_WSTRB 1, HAS_BRESP 1, HAS_RRESP 1" *)
    input wire [15:0] s_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWVALID" *)
    input wire s_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI AWREADY" *)
    output reg s_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WDATA" *)
    input wire [31:0] s_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WSTRB" *)
    input wire [3:0] s_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WVALID" *)
    input wire s_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI WREADY" *)
    output reg s_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BRESP" *)
    output wire [1:0] s_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BVALID" *)
    output reg s_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI BREADY" *)
    input wire s_axi_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARADDR" *)
    input wire [15:0] s_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARVALID" *)
    input wire s_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI ARREADY" *)
    output reg s_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RDATA" *)
    output reg [31:0] s_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RRESP" *)
    output wire [1:0] s_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RVALID" *)
    output reg s_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 S_AXI RREADY" *)
    input wire s_axi_rready,

    output wire [31:0] reset_request
);
    wire write_fire;
    wire read_fire;
    wire [15:0] selected_addr;
    wire [15:0] identity_addr;
    wire [31:0] identity_rdata;

    assign write_fire = !s_axi_bvalid && s_axi_awvalid && s_axi_wvalid;
    assign read_fire = !write_fire && !s_axi_rvalid && s_axi_arvalid;
    assign selected_addr = write_fire ? s_axi_awaddr : s_axi_araddr;
    assign identity_addr = {4'h0, selected_addr[11:0]};
    assign s_axi_bresp = 2'b00;
    assign s_axi_rresp = 2'b00;

    dau_identity_registers #(
        .REGISTER_MAP_VERSION(REGISTER_MAP_VERSION),
        .STREAM_PROTOCOL_VERSION(STREAM_PROTOCOL_VERSION),
        .PLATFORM_ID(PLATFORM_ID),
        .SHELL_VERSION(SHELL_VERSION),
        .BUILD_ID(BUILD_ID),
        .STATUS(STATUS),
        .DMA_CONSTRAINTS(DMA_CONSTRAINTS),
        .OPERATOR_BITMAP(OPERATOR_BITMAP),
        .LANE_COUNT(LANE_COUNT),
        .HOST_OPCODE_BITMAP(HOST_OPCODE_BITMAP),
        .SORT_CAPACITY(SORT_CAPACITY),
        .LAST_ERROR(LAST_ERROR),
        .H2C_BYTES(H2C_BYTES),
        .C2H_BYTES(C2H_BYTES),
        .ROWS(ROWS),
        .BATCHES(BATCHES),
        .STALLS(STALLS)
    ) identity_registers (
        .addr(identity_addr),
        .wen(write_fire),
        .wdata(s_axi_wdata),
        .reset_request(reset_request),
        .rdata(identity_rdata)
    );

    always @(posedge s_axi_aclk) begin
        if (!s_axi_aresetn) begin
            s_axi_awready <= 1'b0;
            s_axi_wready <= 1'b0;
            s_axi_bvalid <= 1'b0;
            s_axi_arready <= 1'b0;
            s_axi_rdata <= 32'h0000_0000;
            s_axi_rvalid <= 1'b0;
        end else begin
            s_axi_awready <= write_fire;
            s_axi_wready <= write_fire;
            s_axi_arready <= read_fire;

            if (write_fire) begin
                s_axi_bvalid <= 1'b1;
            end else if (s_axi_bvalid && s_axi_bready) begin
                s_axi_bvalid <= 1'b0;
            end

            if (read_fire) begin
                s_axi_rdata <= identity_rdata;
                s_axi_rvalid <= 1'b1;
            end else if (s_axi_rvalid && s_axi_rready) begin
                s_axi_rvalid <= 1'b0;
            end
        end
    end
endmodule

`default_nettype wire

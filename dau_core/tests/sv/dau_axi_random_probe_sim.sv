// Bench top: the random-read probe against the AXI RAM sim model.
//
// READ_LATENCY on the RAM stands in for the DRAM round trip. The point of
// the bench is that cycles-per-probe should fall as MAX_OUTSTANDING rises,
// until the pipeline rather than the latency is the limit -- and that a
// depth-1 configuration reproduces the burst reader's behaviour, which is
// what makes the comparison meaningful.

`default_nettype none

module dau_axi_random_probe_sim #(
    parameter int MAX_OUTSTANDING = 8,
    parameter int READ_LATENCY = 16
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        start,
    input  wire logic [31:0] window_base,
    input  wire logic [31:0] window_mask,
    input  wire logic [31:0] probe_count,
    output logic             busy,
    output logic             done,
    output logic [31:0]      elapsed_cycles,
    output logic [15:0]      peak_outstanding,
    output logic [63:0]      read_checksum,
    output logic             slave_error
);

    logic [31:0] araddr;
    logic [7:0]  arlen;
    logic [2:0]  arsize;
    logic [1:0]  arburst;
    logic        arvalid, arready;
    logic [63:0] rdata;
    logic [1:0]  rresp;
    logic        rlast, rvalid, rready;

    dau_axi_random_probe #(
        .ADDR_WIDTH(32),
        .DATA_WIDTH(64),
        .MAX_OUTSTANDING(MAX_OUTSTANDING)
    ) probe (
        .clk(clk), .rst(rst), .start(start),
        .window_base(window_base), .window_mask(window_mask), .probe_count(probe_count),
        .busy(busy), .done(done), .elapsed_cycles(elapsed_cycles),
        .peak_outstanding(peak_outstanding), .read_checksum(read_checksum), .slave_error(slave_error),
        .m_axi_araddr(araddr), .m_axi_arlen(arlen), .m_axi_arsize(arsize), .m_axi_arburst(arburst),
        .m_axi_arvalid(arvalid), .m_axi_arready(arready),
        .m_axi_rdata(rdata), .m_axi_rresp(rresp), .m_axi_rlast(rlast),
        .m_axi_rvalid(rvalid), .m_axi_rready(rready)
    );

    dau_axi_ram_sim #(
        .ADDR_WIDTH(32),
        .MEM_WORDS(4096),
        .READ_LATENCY(READ_LATENCY),
        .DATA_WIDTH(64)
    ) ram (
        .clk(clk), .rst(rst),
        .s_axi_araddr(araddr), .s_axi_arlen(arlen), .s_axi_arsize(arsize), .s_axi_arburst(arburst),
        .s_axi_arvalid(arvalid), .s_axi_arready(arready),
        .s_axi_rdata(rdata), .s_axi_rresp(rresp), .s_axi_rlast(rlast),
        .s_axi_rvalid(rvalid), .s_axi_rready(rready),
        .s_axi_awaddr(32'd0), .s_axi_awlen(8'd0), .s_axi_awsize(3'd3), .s_axi_awburst(2'b01),
        .s_axi_awvalid(1'b0), .s_axi_awready(),
        .s_axi_wdata(64'd0), .s_axi_wstrb(8'hFF), .s_axi_wlast(1'b0),
        .s_axi_wvalid(1'b0), .s_axi_wready(),
        .s_axi_bresp(), .s_axi_bvalid(), .s_axi_bready(1'b1)
    );

endmodule

`default_nettype wire

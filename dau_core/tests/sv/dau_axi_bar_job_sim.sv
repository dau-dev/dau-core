`default_nettype none

// Composed simulation top: dau_axi_stream_job (AXI4 master runner) driving
// the fused bar-aggregation core against dau_axi_ram_sim. This is the
// sim-first stand-in for the DDR-staged shell datapath: the bench loads
// rows into RAM through the backdoor, kicks a job with device addresses,
// and reads the bar records back out of RAM. cfg_row_count mirrors the MM
// shell binding (16-byte rows: input_length_bytes >> 4); bar_count latches
// at the core status fire exactly as the generated shells latch
// PIPELINE_GROUP_COUNT.
module dau_axi_bar_job_sim #(
    parameter int unsigned MEM_WORDS = 65536
) (
    input  wire logic        clk,
    input  wire logic        rst,

    input  wire logic        start,
    input  wire logic [31:0] input_address,
    input  wire logic [31:0] input_length_bytes,
    input  wire logic [31:0] output_address,
    output logic             busy,
    output logic             done,
    output logic             error,
    output logic [7:0]       error_code,
    output logic [31:0]      result_length_bytes,
    output logic [63:0]      bar_count,

    input  wire logic        bd_write,
    input  wire logic [31:0] bd_index,
    input  wire logic [63:0] bd_wdata,
    output logic [63:0]      bd_rdata
);
    logic [31:0] m_axi_araddr;
    logic [7:0]  m_axi_arlen;
    logic [2:0]  m_axi_arsize;
    logic [1:0]  m_axi_arburst;
    logic        m_axi_arvalid;
    logic        m_axi_arready;
    logic [63:0] m_axi_rdata;
    logic [1:0]  m_axi_rresp;
    logic        m_axi_rlast;
    logic        m_axi_rvalid;
    logic        m_axi_rready;
    logic [31:0] m_axi_awaddr;
    logic [7:0]  m_axi_awlen;
    logic [2:0]  m_axi_awsize;
    logic [1:0]  m_axi_awburst;
    logic        m_axi_awvalid;
    logic        m_axi_awready;
    logic [63:0] m_axi_wdata;
    logic [7:0]  m_axi_wstrb;
    logic        m_axi_wlast;
    logic        m_axi_wvalid;
    logic        m_axi_wready;
    logic [1:0]  m_axi_bresp;
    logic        m_axi_bvalid;
    logic        m_axi_bready;

    logic        core_input_valid;
    logic        core_input_ready;
    logic [63:0] core_input_data;
    logic        core_input_last;
    logic        core_output_valid;
    logic        core_output_ready;
    logic [63:0] core_output_data;
    logic        core_output_last;
    logic        core_status_valid;
    logic        core_status_ready;
    logic        core_status_error;
    logic [7:0]  core_status_error_code;
    logic [63:0] core_bar_count;

    dau_axi_stream_job #(
        .ADDR_WIDTH(32),
        .BURST_BEATS(16),
        .LENGTH_ALIGN_BITS(4)
    ) runner (
        .clk(clk),
        .rst(rst),
        .start(start),
        .input_address(input_address),
        .input_length_bytes(input_length_bytes),
        .output_address(output_address),
        .busy(busy),
        .done(done),
        .error(error),
        .error_code(error_code),
        .result_length_bytes(result_length_bytes),
        .m_axi_araddr(m_axi_araddr),
        .m_axi_arlen(m_axi_arlen),
        .m_axi_arsize(m_axi_arsize),
        .m_axi_arburst(m_axi_arburst),
        .m_axi_arvalid(m_axi_arvalid),
        .m_axi_arready(m_axi_arready),
        .m_axi_rdata(m_axi_rdata),
        .m_axi_rresp(m_axi_rresp),
        .m_axi_rlast(m_axi_rlast),
        .m_axi_rvalid(m_axi_rvalid),
        .m_axi_rready(m_axi_rready),
        .m_axi_awaddr(m_axi_awaddr),
        .m_axi_awlen(m_axi_awlen),
        .m_axi_awsize(m_axi_awsize),
        .m_axi_awburst(m_axi_awburst),
        .m_axi_awvalid(m_axi_awvalid),
        .m_axi_awready(m_axi_awready),
        .m_axi_wdata(m_axi_wdata),
        .m_axi_wstrb(m_axi_wstrb),
        .m_axi_wlast(m_axi_wlast),
        .m_axi_wvalid(m_axi_wvalid),
        .m_axi_wready(m_axi_wready),
        .m_axi_bresp(m_axi_bresp),
        .m_axi_bvalid(m_axi_bvalid),
        .m_axi_bready(m_axi_bready),
        .core_input_valid(core_input_valid),
        .core_input_ready(core_input_ready),
        .core_input_data(core_input_data),
        .core_input_last(core_input_last),
        .core_output_valid(core_output_valid),
        .core_output_ready(core_output_ready),
        .core_output_data(core_output_data),
        .core_output_last(core_output_last),
        .core_status_valid(core_status_valid),
        .core_status_ready(core_status_ready),
        .core_status_error(core_status_error),
        .core_status_error_code(core_status_error_code)
    );

    dau_bar_aggregation core (
        .clk(clk),
        .rst(rst),
        .cfg_mode(2'd0),
        .cfg_row_count({36'd0, input_length_bytes[31:4]}),
        .input_valid(core_input_valid),
        .input_ready(core_input_ready),
        .input_data(core_input_data),
        .input_last(core_input_last),
        .output_valid(core_output_valid),
        .output_ready(core_output_ready),
        .output_data(core_output_data),
        .output_last(core_output_last),
        .status_valid(core_status_valid),
        .status_ready(core_status_ready),
        .status_error(core_status_error),
        .status_error_code(core_status_error_code),
        .bar_count(core_bar_count)
    );

    dau_axi_ram_sim #(
        .ADDR_WIDTH(32),
        .MEM_WORDS(MEM_WORDS),
        .READ_LATENCY(4)
    ) ram (
        .clk(clk),
        .rst(rst),
        .s_axi_araddr(m_axi_araddr),
        .s_axi_arlen(m_axi_arlen),
        .s_axi_arsize(m_axi_arsize),
        .s_axi_arburst(m_axi_arburst),
        .s_axi_arvalid(m_axi_arvalid),
        .s_axi_arready(m_axi_arready),
        .s_axi_rdata(m_axi_rdata),
        .s_axi_rresp(m_axi_rresp),
        .s_axi_rlast(m_axi_rlast),
        .s_axi_rvalid(m_axi_rvalid),
        .s_axi_rready(m_axi_rready),
        .s_axi_awaddr(m_axi_awaddr),
        .s_axi_awlen(m_axi_awlen),
        .s_axi_awsize(m_axi_awsize),
        .s_axi_awburst(m_axi_awburst),
        .s_axi_awvalid(m_axi_awvalid),
        .s_axi_awready(m_axi_awready),
        .s_axi_wdata(m_axi_wdata),
        .s_axi_wstrb(m_axi_wstrb),
        .s_axi_wlast(m_axi_wlast),
        .s_axi_wvalid(m_axi_wvalid),
        .s_axi_wready(m_axi_wready),
        .s_axi_bresp(m_axi_bresp),
        .s_axi_bvalid(m_axi_bvalid),
        .s_axi_bready(m_axi_bready),
        .bd_write(bd_write),
        .bd_index(bd_index),
        .bd_wdata(bd_wdata),
        .bd_rdata(bd_rdata)
    );

    always_ff @(posedge clk) begin
        if (rst) begin
            bar_count <= 64'd0;
        end else if (core_status_valid && core_status_ready) begin
            bar_count <= core_bar_count;
        end
    end
endmodule

`default_nettype wire

`default_nettype none

// Composed sorted-scan NoC simulation top: one AXI burst reader scans the
// input window once into the range partitioner, which routes each whole
// row to one of NUM_LANES key-ordered lanes; each lane bitonic-sorts its
// partition and a record writer lands the sorted rows in the lane's own
// output region through the shared write mux. Because the partitions are
// key-ordered and the sorter is stable, the lane regions concatenated in
// lane order are the globally sorted scan — ORDER BY as a streaming job
// (partition -> local sort -> concat, the HARP recipe). Job status
// aggregates: done when the reader and every writer finish; first error
// wins. Lanes that receive no rows close out through the partitioner's
// empty-lane status; a lane partition larger than SORT_CAPACITY reports
// the sorter's ERR_CAPACITY (partition finer). This is the sim-first
// stand-in for a generated sorted-scan shell top.
module dau_axi_sort_noc_sim #(
    parameter int unsigned NUM_LANES = 4,
    parameter int unsigned SORT_CAPACITY = 16,
    parameter int unsigned MEM_WORDS = 65536
) (
    input  wire logic        clk,
    input  wire logic        rst,

    input  wire logic        start,
    input  wire logic [31:0] input_address,
    input  wire logic [31:0] input_length_bytes,
    input  wire logic [(NUM_LANES-1)*32-1:0] cfg_splitters,
    input  wire logic [NUM_LANES-1:0][31:0] lane_output_address,
    output logic             busy,
    output logic             done,
    output logic             error,
    output logic [7:0]       error_code,
    output logic [NUM_LANES-1:0][31:0] lane_result_length_bytes,

    input  wire logic        bd_write,
    input  wire logic [31:0] bd_index,
    input  wire logic [63:0] bd_wdata,
    output logic [63:0]      bd_rdata
);
    // reader
    logic [31:0] rd_araddr;
    logic [7:0]  rd_arlen;
    logic [2:0]  rd_arsize;
    logic [1:0]  rd_arburst;
    logic        rd_arvalid;
    logic        rd_arready;
    logic [63:0] rd_rdata;
    logic [1:0]  rd_rresp;
    logic        rd_rlast;
    logic        rd_rvalid;
    logic        rd_rready;
    logic        reader_busy;
    logic        reader_done;
    logic        reader_error;
    logic [7:0]  reader_error_code;

    // scan stream into the partitioner
    logic        scan_valid;
    logic        scan_ready;
    logic [63:0] scan_data;
    logic        scan_last;

    // partitioner lane outputs (flat)
    logic [NUM_LANES-1:0]      part_out_valid;
    logic [NUM_LANES-1:0]      part_out_ready;
    logic [NUM_LANES*64-1:0]   part_out_data;
    logic [NUM_LANES-1:0]      part_out_last;
    logic [NUM_LANES-1:0]      part_status_valid;
    logic [NUM_LANES-1:0]      part_status_ready;
    logic [NUM_LANES-1:0]      part_status_error;
    logic [NUM_LANES*8-1:0]    part_status_error_code;

    // per-lane sorter plumbing
    logic [NUM_LANES-1:0] sort_out_valid;
    logic [NUM_LANES-1:0] sort_out_ready;
    logic [63:0] sort_out_data [NUM_LANES];
    logic [NUM_LANES-1:0] sort_out_last;
    logic [NUM_LANES-1:0] sort_status_valid;
    logic [NUM_LANES-1:0] sort_status_ready;
    logic [NUM_LANES-1:0] sort_status_error;
    logic [7:0] sort_status_error_code [NUM_LANES];

    logic [NUM_LANES-1:0] unit_status_valid;
    logic [NUM_LANES-1:0] unit_status_ready;
    logic [NUM_LANES-1:0] unit_status_error;
    logic [7:0] unit_status_error_code [NUM_LANES];

    logic [NUM_LANES-1:0] writer_busy;
    logic [NUM_LANES-1:0] writer_done;
    logic [NUM_LANES-1:0] writer_error;
    logic [7:0] writer_error_code [NUM_LANES];

    logic [NUM_LANES-1:0][31:0] wr_awaddr;
    logic [NUM_LANES-1:0][7:0] wr_awlen;
    logic [NUM_LANES-1:0] wr_awvalid;
    logic [NUM_LANES-1:0] wr_awready;
    logic [NUM_LANES-1:0][63:0] wr_wdata;
    logic [NUM_LANES-1:0] wr_wlast;
    logic [NUM_LANES-1:0] wr_wvalid;
    logic [NUM_LANES-1:0] wr_wready;
    logic [1:0] wr_bresp;
    logic [NUM_LANES-1:0] wr_bvalid;
    logic [NUM_LANES-1:0] wr_bready;

    // muxed write master toward the RAM
    logic [31:0] mx_awaddr;
    logic [7:0]  mx_awlen;
    logic [2:0]  mx_awsize;
    logic [1:0]  mx_awburst;
    logic        mx_awvalid;
    logic        mx_awready;
    logic [63:0] mx_wdata;
    logic [7:0]  mx_wstrb;
    logic        mx_wlast;
    logic        mx_wvalid;
    logic        mx_wready;
    logic [1:0]  mx_bresp;
    logic        mx_bvalid;
    logic        mx_bready;

    // recover mid-stream lanes after an error before the next job
    logic prev_done;
    logic pipeline_error_reset;
    logic lane_rst;
    assign lane_rst = rst || pipeline_error_reset;

    // the 16-byte row grid is enforced before any unit starts: a rejected
    // length must not leave the writers waiting on a status that will
    // never come
    logic length_fail;
    logic length_ok;
    logic unit_start;
    assign length_ok = (input_length_bytes != 32'd0) && (input_length_bytes[3:0] == 4'd0);
    assign unit_start = start && length_ok;

    dau_axi_burst_reader #(
        .ADDR_WIDTH(32),
        .BURST_BEATS(16),
        .LENGTH_ALIGN_BITS(4)
    ) reader (
        .clk(clk),
        .rst(rst),
        .start(unit_start),
        .read_address(input_address),
        .read_length_bytes(input_length_bytes),
        .busy(reader_busy),
        .done(reader_done),
        .error(reader_error),
        .error_code(reader_error_code),
        .m_axi_araddr(rd_araddr),
        .m_axi_arlen(rd_arlen),
        .m_axi_arsize(rd_arsize),
        .m_axi_arburst(rd_arburst),
        .m_axi_arvalid(rd_arvalid),
        .m_axi_arready(rd_arready),
        .m_axi_rdata(rd_rdata),
        .m_axi_rresp(rd_rresp),
        .m_axi_rlast(rd_rlast),
        .m_axi_rvalid(rd_rvalid),
        .m_axi_rready(rd_rready),
        .stream_valid(scan_valid),
        .stream_ready(scan_ready),
        .stream_data(scan_data),
        .stream_last(scan_last),
        // boundary debug taps (BAR-mapped on hardware; unused in this sim)
        .dbg_first_stream_word(),
        .dbg_first_araddr(),
        .dbg_beats_while_idle(),
        .dbg_final_fifo_count()
    );

    dau_int32_range_partitioner #(
        .NUM_PARTITIONS(NUM_LANES)
    ) partitioner (
        .clk(clk),
        .rst(lane_rst),
        .cfg_splitters(cfg_splitters),
        .input_valid(scan_valid),
        .input_ready(scan_ready),
        .input_data(scan_data),
        .input_last(scan_last),
        .output_valid(part_out_valid),
        .output_ready(part_out_ready),
        .output_data(part_out_data),
        .output_last(part_out_last),
        .status_valid(part_status_valid),
        .status_ready(part_status_ready),
        .status_error(part_status_error),
        .status_error_code(part_status_error_code)
    );

    for (genvar lane = 0; lane < NUM_LANES; lane++) begin : lanes
        dau_int32_bitonic_sorter #(
            .CAPACITY(SORT_CAPACITY)
        ) sorter (
            .clk(clk),
            .rst(lane_rst),
            .input_valid(part_out_valid[lane]),
            .input_ready(part_out_ready[lane]),
            .input_data(part_out_data[lane*64 +: 64]),
            .input_last(part_out_last[lane]),
            .output_valid(sort_out_valid[lane]),
            .output_ready(sort_out_ready[lane]),
            .output_data(sort_out_data[lane]),
            .output_last(sort_out_last[lane]),
            .status_valid(sort_status_valid[lane]),
            .status_ready(sort_status_ready[lane]),
            .status_error(sort_status_error[lane]),
            .status_error_code(sort_status_error_code[lane])
        );

        // one lane closes out through exactly one of the two producers:
        // the sorter when rows flowed (success after drain, or its error),
        // the partitioner when none did (empty-lane success, or torn)
        assign unit_status_valid[lane] = sort_status_valid[lane] || part_status_valid[lane];
        assign unit_status_error[lane] = part_status_valid[lane] ? part_status_error[lane] : sort_status_error[lane];
        assign unit_status_error_code[lane] = part_status_valid[lane] ? part_status_error_code[lane*8 +: 8] : sort_status_error_code[lane];
        assign part_status_ready[lane] = unit_status_ready[lane] && part_status_valid[lane];
        assign sort_status_ready[lane] = unit_status_ready[lane] && !part_status_valid[lane];

        dau_axi_record_writer #(
            .ADDR_WIDTH(32),
            .BURST_BEATS(16)
        ) writer (
            .clk(clk),
            .rst(rst),
            .start(unit_start),
            .output_address(lane_output_address[lane]),
            .busy(writer_busy[lane]),
            .done(writer_done[lane]),
            .error(writer_error[lane]),
            .error_code(writer_error_code[lane]),
            .result_length_bytes(lane_result_length_bytes[lane]),
            .m_axi_awaddr(wr_awaddr[lane]),
            .m_axi_awlen(wr_awlen[lane]),
            .m_axi_awsize(),
            .m_axi_awburst(),
            .m_axi_awvalid(wr_awvalid[lane]),
            .m_axi_awready(wr_awready[lane]),
            .m_axi_wdata(wr_wdata[lane]),
            .m_axi_wstrb(),
            .m_axi_wlast(wr_wlast[lane]),
            .m_axi_wvalid(wr_wvalid[lane]),
            .m_axi_wready(wr_wready[lane]),
            .m_axi_bresp(wr_bresp),
            .m_axi_bvalid(wr_bvalid[lane]),
            .m_axi_bready(wr_bready[lane]),
            .record_valid(sort_out_valid[lane]),
            .record_ready(sort_out_ready[lane]),
            .record_data(sort_out_data[lane]),
            .record_last(sort_out_last[lane]),
            .status_valid(unit_status_valid[lane]),
            .status_ready(unit_status_ready[lane]),
            .status_error(unit_status_error[lane]),
            .status_error_code(unit_status_error_code[lane])
        );
    end

    dau_axi_write_mux #(
        .NUM_INPUTS(NUM_LANES),
        .ADDR_WIDTH(32)
    ) write_mux (
        .clk(clk),
        .rst(rst),
        .s_awaddr(wr_awaddr),
        .s_awlen(wr_awlen),
        .s_awvalid(wr_awvalid),
        .s_awready(wr_awready),
        .s_wdata(wr_wdata),
        .s_wlast(wr_wlast),
        .s_wvalid(wr_wvalid),
        .s_wready(wr_wready),
        .s_bresp(wr_bresp),
        .s_bvalid(wr_bvalid),
        .s_bready(wr_bready),
        .m_axi_awaddr(mx_awaddr),
        .m_axi_awlen(mx_awlen),
        .m_axi_awsize(mx_awsize),
        .m_axi_awburst(mx_awburst),
        .m_axi_awvalid(mx_awvalid),
        .m_axi_awready(mx_awready),
        .m_axi_wdata(mx_wdata),
        .m_axi_wstrb(mx_wstrb),
        .m_axi_wlast(mx_wlast),
        .m_axi_wvalid(mx_wvalid),
        .m_axi_wready(mx_wready),
        .m_axi_bresp(mx_bresp),
        .m_axi_bvalid(mx_bvalid),
        .m_axi_bready(mx_bready)
    );

    dau_axi_ram_sim #(
        .ADDR_WIDTH(32),
        .MEM_WORDS(MEM_WORDS),
        .READ_LATENCY(4)
    ) ram (
        .clk(clk),
        .rst(rst),
        .s_axi_araddr(rd_araddr),
        .s_axi_arlen(rd_arlen),
        .s_axi_arsize(rd_arsize),
        .s_axi_arburst(rd_arburst),
        .s_axi_arvalid(rd_arvalid),
        .s_axi_arready(rd_arready),
        .s_axi_rdata(rd_rdata),
        .s_axi_rresp(rd_rresp),
        .s_axi_rlast(rd_rlast),
        .s_axi_rvalid(rd_rvalid),
        .s_axi_rready(rd_rready),
        .s_axi_awaddr(mx_awaddr),
        .s_axi_awlen(mx_awlen),
        .s_axi_awsize(mx_awsize),
        .s_axi_awburst(mx_awburst),
        .s_axi_awvalid(mx_awvalid),
        .s_axi_awready(mx_awready),
        .s_axi_wdata(mx_wdata),
        .s_axi_wstrb(mx_wstrb),
        .s_axi_wlast(mx_wlast),
        .s_axi_wvalid(mx_wvalid),
        .s_axi_wready(mx_wready),
        .s_axi_bresp(mx_bresp),
        .s_axi_bvalid(mx_bvalid),
        .s_axi_bready(mx_bready),
        .bd_write(bd_write),
        .bd_index(bd_index),
        .bd_wdata(bd_wdata),
        .bd_rdata(bd_rdata)
    );

    assign busy = reader_busy || (|writer_busy);
    assign done = length_fail || (reader_done && (&writer_done));

    always_comb begin
        if (length_fail) begin
            error = 1'b1;
            error_code = 8'hFE;
        end else begin
            error = reader_error;
            error_code = reader_error ? reader_error_code : 8'd0;
            for (int unsigned lane = 0; lane < NUM_LANES; lane++) begin
                if (!error && writer_error[lane]) begin
                    error = 1'b1;
                    error_code = writer_error_code[lane];
                end
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            prev_done <= 1'b1;
            pipeline_error_reset <= 1'b0;
            length_fail <= 1'b0;
        end else begin
            prev_done <= done;
            pipeline_error_reset <= done && !prev_done && error;
            if (start) begin
                length_fail <= !length_ok;
            end
        end
    end
endmodule

`default_nettype wire

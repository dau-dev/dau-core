`default_nettype none

// DDR-staged stream job runner: an AXI4 master that streams a job's input
// window from memory through a tile and writes the tile's output stream
// back to memory.
//
// Read engine: single-outstanding INCR bursts (up to BURST_BEATS x 64-bit),
// asserting input_last on the final beat of the final burst. Write engine:
// buffers tile output words and flushes INCR bursts (partial burst at
// stream end); done waits for the final write response. Addresses and the
// byte length come from the job registers (the input length must be a
// multiple of 8 bytes; tiles impose their own row grids).
//
// Errors: tile status errors surface with the tile's code;
// non-OKAY AXI responses surface as ERR_AXI_READ / ERR_AXI_WRITE.
module dau_axi_stream_job #(
    parameter int unsigned ADDR_WIDTH = 32,
    parameter int unsigned BURST_BEATS = 16,
    parameter int unsigned LENGTH_ALIGN_BITS = 3
) (
    input  wire logic        clk,
    input  wire logic        rst,

    input  wire logic        start,
    input  wire logic [ADDR_WIDTH-1:0] input_address,
    input  wire logic [31:0] input_length_bytes,
    input  wire logic [ADDR_WIDTH-1:0] output_address,
    output logic             busy,
    output logic             done,
    output logic             error,
    output logic [7:0]       error_code,
    output logic [31:0]      result_length_bytes,

    // AXI4 master (64-bit data, single ID)
    output logic [ADDR_WIDTH-1:0] m_axi_araddr,
    output logic [7:0]       m_axi_arlen,
    output logic [2:0]       m_axi_arsize,
    output logic [1:0]       m_axi_arburst,
    output logic             m_axi_arvalid,
    input  wire logic        m_axi_arready,
    input  wire logic [63:0] m_axi_rdata,
    input  wire logic [1:0]  m_axi_rresp,
    input  wire logic        m_axi_rlast,
    input  wire logic        m_axi_rvalid,
    output logic             m_axi_rready,
    output logic [ADDR_WIDTH-1:0] m_axi_awaddr,
    output logic [7:0]       m_axi_awlen,
    output logic [2:0]       m_axi_awsize,
    output logic [1:0]       m_axi_awburst,
    output logic             m_axi_awvalid,
    input  wire logic        m_axi_awready,
    output logic [63:0]      m_axi_wdata,
    output logic [7:0]       m_axi_wstrb,
    output logic             m_axi_wlast,
    output logic             m_axi_wvalid,
    input  wire logic        m_axi_wready,
    input  wire logic [1:0]  m_axi_bresp,
    input  wire logic        m_axi_bvalid,
    output logic             m_axi_bready,

    // tile stream
    output logic             tile_input_valid,
    input  wire logic        tile_input_ready,
    output logic [63:0]      tile_input_data,
    output logic             tile_input_last,
    input  wire logic        tile_output_valid,
    output logic             tile_output_ready,
    input  wire logic [63:0] tile_output_data,
    input  wire logic        tile_output_last,
    input  wire logic        tile_status_valid,
    output logic             tile_status_ready,
    input  wire logic        tile_status_error,
    input  wire logic [7:0]  tile_status_error_code
);
    localparam logic [7:0] ERR_JOB_LENGTH = 8'hFE;
    localparam logic [7:0] ERR_AXI_READ = 8'hFC;
    localparam logic [7:0] ERR_AXI_WRITE = 8'hFB;

    localparam int unsigned BURST_BITS = $clog2(BURST_BEATS);

    typedef enum logic [2:0] {
        JOB_IDLE,
        JOB_RUN,
        JOB_WAIT_STATUS,
        JOB_DRAIN_WRITES,
        JOB_FINISH
    } job_state_t;

    typedef enum logic [1:0] {
        RD_IDLE,
        RD_ADDR,
        RD_DATA,
        RD_DONE
    } read_state_t;

    typedef enum logic [1:0] {
        WR_COLLECT,
        WR_ADDR,
        WR_DATA
    } write_state_t;

    job_state_t job_state;
    read_state_t read_state;
    write_state_t write_state;

    logic [ADDR_WIDTH-1:0] read_address;
    logic [31:0] read_words_remaining;
    logic [7:0]  read_burst_len;  // beats-1 of the in-flight burst
    logic        read_error;

    logic [ADDR_WIDTH-1:0] write_address;
    logic [63:0] write_buffer [BURST_BEATS];
    logic [BURST_BITS:0] write_fill;
    logic [BURST_BITS:0] write_index;
    logic        flush_pending;
    logic [31:0] words_written;
    logic [7:0]  outstanding_writes;
    logic        write_error;
    logic        tile_error_seen;
    logic [7:0]  tile_error_code;

    // fixed AXI attributes
    assign m_axi_arsize = 3'b011;   // 8 bytes
    assign m_axi_arburst = 2'b01;   // INCR
    assign m_axi_awsize = 3'b011;
    assign m_axi_awburst = 2'b01;
    assign m_axi_wstrb = 8'hFF;
    assign m_axi_bready = 1'b1;

    // read data flows straight to the tile
    assign tile_input_valid = (read_state == RD_DATA) && m_axi_rvalid;
    assign tile_input_data = m_axi_rdata;
    assign tile_input_last = (read_words_remaining == 32'd1);
    assign m_axi_rready = (read_state == RD_DATA) && tile_input_ready;

    // the write engine owns tile output acceptance
    assign tile_output_ready = (job_state == JOB_RUN) && (write_state == WR_COLLECT) && (write_fill != BURST_BEATS[BURST_BITS:0]);
    assign tile_status_ready = job_state == JOB_WAIT_STATUS;

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            case (job_state)
                JOB_IDLE: begin
                    if (start) begin
                        done <= 1'b0;
                        error <= 1'b0;
                        error_code <= 8'd0;
                        result_length_bytes <= 32'd0;
                        read_error <= 1'b0;
                        write_error <= 1'b0;
                        tile_error_seen <= 1'b0;
                        tile_error_code <= 8'd0;
                        words_written <= 32'd0;
                        write_fill <= '0;
                        write_index <= '0;
                        flush_pending <= 1'b0;
                        outstanding_writes <= 8'd0;
                        if ((input_length_bytes == 32'd0)
                                || (input_length_bytes[LENGTH_ALIGN_BITS-1:0] != '0)) begin
                            done <= 1'b1;
                            error <= 1'b1;
                            error_code <= ERR_JOB_LENGTH;
                        end else begin
                            busy <= 1'b1;
                            read_address <= input_address;
                            read_words_remaining <= {3'd0, input_length_bytes[31:3]};
                            write_address <= output_address;
                            read_state <= RD_ADDR;
                            write_state <= WR_COLLECT;
                            job_state <= JOB_RUN;
                        end
                    end
                end
                JOB_RUN: begin
                    run_read_engine();
                    run_write_engine();
                    if ((read_state == RD_DONE) && tile_status_valid) begin
                        job_state <= JOB_WAIT_STATUS;
                    end
                end
                JOB_WAIT_STATUS: begin
                    run_write_engine();
                    if (tile_status_valid) begin
                        tile_error_seen <= tile_status_error;
                        tile_error_code <= tile_status_error_code;
                        if ((write_fill != '0) && (write_state == WR_COLLECT)) begin
                            flush_pending <= 1'b1;
                        end
                        job_state <= JOB_DRAIN_WRITES;
                    end
                end
                JOB_DRAIN_WRITES: begin
                    run_write_engine();
                    if ((write_state == WR_COLLECT) && !flush_pending && (write_fill == '0) && (outstanding_writes == 8'd0)) begin
                        job_state <= JOB_FINISH;
                    end
                end
                JOB_FINISH: begin
                    busy <= 1'b0;
                    done <= 1'b1;
                    error <= tile_error_seen || read_error || write_error;
                    error_code <= tile_error_seen ? tile_error_code : (read_error ? ERR_AXI_READ : (write_error ? ERR_AXI_WRITE : 8'd0));
                    result_length_bytes <= (tile_error_seen || read_error || write_error) ? 32'd0 : (words_written << 3);
                    job_state <= JOB_IDLE;
                    read_state <= RD_IDLE;
                end
                default: begin
                    reset_state();
                end
            endcase

            if (m_axi_bvalid) begin
                outstanding_writes <= outstanding_writes - 8'd1
                    + ((m_axi_awvalid && m_axi_awready) ? 8'd1 : 8'd0);
                if (m_axi_bresp != 2'b00) begin
                    write_error <= 1'b1;
                end
            end else if (m_axi_awvalid && m_axi_awready) begin
                outstanding_writes <= outstanding_writes + 8'd1;
            end
        end
    end

    task automatic run_read_engine;
        begin
            case (read_state)
                RD_ADDR: begin
                    if (read_words_remaining == 32'd0) begin
                        read_state <= RD_DONE;
                        m_axi_arvalid <= 1'b0;
                    end else if (!m_axi_arvalid) begin
                        m_axi_araddr <= read_address;
                        m_axi_arlen <= (read_words_remaining >= BURST_BEATS) ? 8'(BURST_BEATS - 1) : 8'(read_words_remaining - 1);
                        m_axi_arvalid <= 1'b1;
                    end else if (m_axi_arready) begin
                        m_axi_arvalid <= 1'b0;
                        read_burst_len <= m_axi_arlen;
                        read_state <= RD_DATA;
                    end
                end
                RD_DATA: begin
                    if (m_axi_rvalid && m_axi_rready) begin
                        if (m_axi_rresp != 2'b00) begin
                            read_error <= 1'b1;
                        end
                        read_words_remaining <= read_words_remaining - 32'd1;
                        if (m_axi_rlast) begin
                            read_address <= read_address + (({24'd0, read_burst_len} + 32'd1) << 3);
                            read_state <= RD_ADDR;
                        end
                    end
                end
                RD_IDLE, RD_DONE: ;
                default: ;
            endcase
        end
    endtask

    task automatic run_write_engine;
        begin
            case (write_state)
                WR_COLLECT: begin
                    if (tile_output_valid && tile_output_ready) begin
                        write_buffer[write_fill[BURST_BITS-1:0]] <= tile_output_data;
                        write_fill <= write_fill + 1'b1;
                        if ((write_fill + 1'b1 == BURST_BEATS[BURST_BITS:0]) || tile_output_last) begin
                            write_state <= WR_ADDR;
                        end
                    end else if (flush_pending && (write_fill != '0)) begin
                        flush_pending <= 1'b0;
                        write_state <= WR_ADDR;
                    end else if (flush_pending) begin
                        flush_pending <= 1'b0;
                    end
                end
                WR_ADDR: begin
                    if (!m_axi_awvalid) begin
                        m_axi_awaddr <= write_address;
                        m_axi_awlen <= 8'(write_fill - 1'b1);
                        m_axi_awvalid <= 1'b1;
                        write_index <= '0;
                    end else if (m_axi_awready) begin
                        m_axi_awvalid <= 1'b0;
                        write_state <= WR_DATA;
                    end
                end
                WR_DATA: begin
                    if (!m_axi_wvalid || m_axi_wready) begin
                        if (write_index != write_fill) begin
                            m_axi_wdata <= write_buffer[write_index[BURST_BITS-1:0]];
                            m_axi_wlast <= (write_index + 1'b1) == write_fill;
                            m_axi_wvalid <= 1'b1;
                            write_index <= write_index + 1'b1;
                        end else begin
                            m_axi_wvalid <= 1'b0;
                            m_axi_wlast <= 1'b0;
                            write_address <= write_address + ({{(ADDR_WIDTH - BURST_BITS - 1) {1'b0}}, write_fill} << 3);
                            words_written <= words_written + {{(32 - BURST_BITS - 1) {1'b0}}, write_fill};
                            write_fill <= '0;
                            write_state <= WR_COLLECT;
                        end
                    end
                end
                default: ;
            endcase
        end
    endtask

    task automatic reset_state;
        begin
            job_state <= JOB_IDLE;
            read_state <= RD_IDLE;
            write_state <= WR_COLLECT;
            busy <= 1'b0;
            done <= 1'b0;
            error <= 1'b0;
            error_code <= 8'd0;
            result_length_bytes <= 32'd0;
            read_address <= '0;
            read_words_remaining <= 32'd0;
            read_burst_len <= 8'd0;
            read_error <= 1'b0;
            write_address <= '0;
            write_fill <= '0;
            write_index <= '0;
            flush_pending <= 1'b0;
            words_written <= 32'd0;
            outstanding_writes <= 8'd0;
            write_error <= 1'b0;
            tile_error_seen <= 1'b0;
            tile_error_code <= 8'd0;
            m_axi_araddr <= '0;
            m_axi_arlen <= 8'd0;
            m_axi_arvalid <= 1'b0;
            m_axi_awaddr <= '0;
            m_axi_awlen <= 8'd0;
            m_axi_awvalid <= 1'b0;
            m_axi_wdata <= 64'd0;
            m_axi_wlast <= 1'b0;
            m_axi_wvalid <= 1'b0;
        end
    endtask
endmodule

`default_nettype wire

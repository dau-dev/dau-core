`default_nettype none

// AXI4 burst write master draining one tile's record stream to a memory
// region: the write half of dau_axi_stream_job as a standalone unit so
// NoC tops give every tile its own output region and let the shell
// interconnect arbitrate. Buffers up to BURST_BEATS words and flushes
// INCR bursts (partial burst at stream end); done waits for the final
// write response and the consumed status. Status errors surface with the
// producer's code; non-OKAY write responses surface as ERR_AXI_WRITE.
module dau_axi_record_writer #(
    parameter int unsigned ADDR_WIDTH = 32,
    parameter int unsigned BURST_BEATS = 16
) (
    input  wire logic        clk,
    input  wire logic        rst,

    input  wire logic        start,
    input  wire logic [ADDR_WIDTH-1:0] output_address,
    output logic             busy,
    output logic             done,
    output logic             error,
    output logic [7:0]       error_code,
    output logic [31:0]      result_length_bytes,

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

    input  wire logic        record_valid,
    output logic             record_ready,
    input  wire logic [63:0] record_data,
    input  wire logic        record_last,
    input  wire logic        status_valid,
    output logic             status_ready,
    input  wire logic        status_error,
    input  wire logic [7:0]  status_error_code
);
    localparam logic [7:0] ERR_AXI_WRITE = 8'hFB;

    localparam int unsigned BURST_BITS = $clog2(BURST_BEATS);

    typedef enum logic [2:0] {
        UNIT_IDLE,
        UNIT_RUN,
        UNIT_DRAIN,
        UNIT_FINISH
    } unit_state_t;

    typedef enum logic [1:0] {
        WR_COLLECT,
        WR_ADDR,
        WR_DATA
    } write_state_t;

    unit_state_t unit_state;
    write_state_t write_state;

    logic [ADDR_WIDTH-1:0] write_address;
    logic [63:0] write_buffer [BURST_BEATS];
    logic [BURST_BITS:0] write_fill;
    logic [BURST_BITS:0] write_index;
    logic        flush_pending;
    logic [31:0] words_written;
    logic [7:0]  outstanding_writes;
    logic        write_error;
    logic        producer_error;
    logic [7:0]  producer_error_code;

    assign m_axi_awsize = 3'b011;   // 8 bytes
    assign m_axi_awburst = 2'b01;   // INCR
    assign m_axi_wstrb = 8'hFF;
    assign m_axi_bready = 1'b1;

    assign record_ready = (unit_state == UNIT_RUN) && (write_state == WR_COLLECT) && (write_fill != BURST_BEATS[BURST_BITS:0]);
    assign status_ready = unit_state == UNIT_RUN;

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            case (unit_state)
                UNIT_IDLE: begin
                    if (start) begin
                        done <= 1'b0;
                        error <= 1'b0;
                        error_code <= 8'd0;
                        result_length_bytes <= 32'd0;
                        write_error <= 1'b0;
                        producer_error <= 1'b0;
                        producer_error_code <= 8'd0;
                        words_written <= 32'd0;
                        write_fill <= '0;
                        write_index <= '0;
                        flush_pending <= 1'b0;
                        outstanding_writes <= 8'd0;
                        busy <= 1'b1;
                        write_address <= output_address;
                        write_state <= WR_COLLECT;
                        unit_state <= UNIT_RUN;
                    end
                end
                UNIT_RUN: begin
                    run_write_engine();
                    if (status_valid) begin
                        producer_error <= status_error;
                        producer_error_code <= status_error_code;
                        if ((write_fill != '0) && (write_state == WR_COLLECT)) begin
                            flush_pending <= 1'b1;
                        end
                        unit_state <= UNIT_DRAIN;
                    end
                end
                UNIT_DRAIN: begin
                    run_write_engine();
                    if ((write_state == WR_COLLECT) && !flush_pending && (write_fill == '0) && (outstanding_writes == 8'd0)) begin
                        unit_state <= UNIT_FINISH;
                    end
                end
                UNIT_FINISH: begin
                    busy <= 1'b0;
                    done <= 1'b1;
                    error <= producer_error || write_error;
                    error_code <= producer_error ? producer_error_code : (write_error ? ERR_AXI_WRITE : 8'd0);
                    result_length_bytes <= (producer_error || write_error) ? 32'd0 : (words_written << 3);
                    unit_state <= UNIT_IDLE;
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

    task automatic run_write_engine;
        begin
            case (write_state)
                WR_COLLECT: begin
                    if (record_valid && record_ready) begin
                        write_buffer[write_fill[BURST_BITS-1:0]] <= record_data;
                        write_fill <= write_fill + 1'b1;
                        if ((write_fill + 1'b1 == BURST_BEATS[BURST_BITS:0]) || record_last) begin
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
            unit_state <= UNIT_IDLE;
            write_state <= WR_COLLECT;
            busy <= 1'b0;
            done <= 1'b0;
            error <= 1'b0;
            error_code <= 8'd0;
            result_length_bytes <= 32'd0;
            write_address <= '0;
            write_fill <= '0;
            write_index <= '0;
            flush_pending <= 1'b0;
            words_written <= 32'd0;
            outstanding_writes <= 8'd0;
            write_error <= 1'b0;
            producer_error <= 1'b0;
            producer_error_code <= 8'd0;
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

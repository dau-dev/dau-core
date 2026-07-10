`default_nettype none

// AXI4 burst read master streaming a memory window as a 64-bit stream:
// the read half of dau_axi_stream_job as a standalone unit so NoC tops
// can fan one scan out to many consumers. Pipelined INCR bursts with
// multiple in flight, credit-reserved against an internal FIFO (rready
// never mid-burst-stalls); input_last asserts on the final word. The host
// keeps addresses 4 KiB-aligned so bursts never cross a 4 KiB boundary.
module dau_axi_burst_reader #(
    parameter int unsigned ADDR_WIDTH = 32,
    parameter int unsigned BURST_BEATS = 16,
    parameter int unsigned LENGTH_ALIGN_BITS = 3
) (
    input  wire logic        clk,
    input  wire logic        rst,

    input  wire logic        start,
    input  wire logic [ADDR_WIDTH-1:0] read_address,
    input  wire logic [31:0] read_length_bytes,
    output logic             busy,
    output logic             done,
    output logic             error,
    output logic [7:0]       error_code,

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

    output logic             stream_valid,
    input  wire logic        stream_ready,
    output logic [63:0]      stream_data,
    output logic             stream_last,

    // debug taps: state of the last completed run and boundary anomalies
    output logic [63:0]      dbg_first_stream_word,
    output logic [31:0]      dbg_first_araddr,
    output logic [31:0]      dbg_beats_while_idle,
    output logic [31:0]      dbg_final_fifo_count
);
    localparam logic [7:0] ERR_JOB_LENGTH = 8'hFE;
    localparam logic [7:0] ERR_AXI_READ = 8'hFC;

    localparam int unsigned FIFO_DEPTH = 8 * BURST_BEATS;
    localparam int unsigned FIFO_BITS = $clog2(FIFO_DEPTH);

    logic running;
    logic [ADDR_WIDTH-1:0] next_address;
    logic [31:0] ar_words_remaining;      // words not yet covered by an issued AR
    logic [31:0] stream_words_remaining;  // words not yet delivered downstream
    logic [31:0] beats_unarrived;         // issued words whose data has not landed
    logic        read_error;
    logic [63:0] read_fifo [FIFO_DEPTH];
    logic [FIFO_BITS-1:0] fifo_wr_ptr;
    logic [FIFO_BITS-1:0] fifo_rd_ptr;
    logic [FIFO_BITS:0] fifo_count;
    logic [FIFO_BITS:0] fifo_space;       // free minus in-flight reservations

    logic rbeat_fire;
    logic consume_fire;
    logic issue_fire;
    logic [7:0] next_burst_beats;
    logic dbg_first_word_seen;
    logic dbg_first_araddr_seen;

    assign m_axi_arsize = 3'b011;   // 8 bytes
    assign m_axi_arburst = 2'b01;   // INCR
    // credit reservation should make a full FIFO unreachable while beats
    // are in flight; the rready gate makes overwriting unconsumed slots
    // physically impossible regardless (deasserting mid-burst is legal)
    assign m_axi_rready = fifo_count != (FIFO_BITS + 1)'(FIFO_DEPTH);

    assign stream_valid = running && (fifo_count != '0);
    assign stream_data = read_fifo[fifo_rd_ptr];
    assign stream_last = stream_words_remaining == 32'd1;

    assign rbeat_fire = m_axi_rvalid && m_axi_rready;
    assign consume_fire = stream_valid && stream_ready;
    assign next_burst_beats = (ar_words_remaining >= BURST_BEATS) ? 8'(BURST_BEATS) : ar_words_remaining[7:0];
    assign issue_fire = running && (ar_words_remaining != 32'd0) && !m_axi_arvalid
        && (fifo_space >= {{(FIFO_BITS - 7) {1'b0}}, next_burst_beats});

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (issue_fire) begin
                m_axi_araddr <= next_address;
                m_axi_arlen <= next_burst_beats - 8'd1;
                m_axi_arvalid <= 1'b1;
                next_address <= next_address + ({24'd0, next_burst_beats} << 3);
                ar_words_remaining <= ar_words_remaining - {24'd0, next_burst_beats};
            end else if (m_axi_arvalid && m_axi_arready) begin
                m_axi_arvalid <= 1'b0;
            end
            beats_unarrived <= beats_unarrived
                + (issue_fire ? {24'd0, next_burst_beats} : 32'd0)
                - {31'd0, rbeat_fire && running};

            // beats landing outside a run are orphans (a wedged or aborted
            // job left responses in the interconnect): discard them so the
            // next job's stream cannot start with another job's data
            if (rbeat_fire && running) begin
                read_fifo[fifo_wr_ptr] <= m_axi_rdata;
                fifo_wr_ptr <= fifo_wr_ptr + 1'b1;
                if (m_axi_rresp != 2'b00) begin
                    read_error <= 1'b1;
                end
            end
            if (consume_fire) begin
                fifo_rd_ptr <= fifo_rd_ptr + 1'b1;
                stream_words_remaining <= stream_words_remaining - 32'd1;
            end
            fifo_count <= fifo_count
                + {{FIFO_BITS {1'b0}}, rbeat_fire && running}
                - {{FIFO_BITS {1'b0}}, consume_fire};
            fifo_space <= fifo_space
                + {{FIFO_BITS {1'b0}}, consume_fire}
                - (issue_fire ? {{(FIFO_BITS - 7) {1'b0}}, next_burst_beats} : '0);

            if (rbeat_fire && !running) begin
                dbg_beats_while_idle <= dbg_beats_while_idle + 32'd1;
            end
            if (consume_fire && !dbg_first_word_seen) begin
                dbg_first_word_seen <= 1'b1;
                dbg_first_stream_word <= stream_data;
            end
            if (issue_fire && !dbg_first_araddr_seen) begin
                dbg_first_araddr_seen <= 1'b1;
                dbg_first_araddr <= next_address[31:0];
            end

            if (!running) begin
                if (start) begin
                    done <= 1'b0;
                    error <= 1'b0;
                    error_code <= 8'd0;
                    read_error <= 1'b0;
                    dbg_first_word_seen <= 1'b0;
                    dbg_first_araddr_seen <= 1'b0;
                    if ((read_length_bytes == 32'd0)
                            || (read_length_bytes[LENGTH_ALIGN_BITS-1:0] != '0)) begin
                        done <= 1'b1;
                        error <= 1'b1;
                        error_code <= ERR_JOB_LENGTH;
                    end else begin
                        busy <= 1'b1;
                        running <= 1'b1;
                        next_address <= read_address;
                        ar_words_remaining <= {3'd0, read_length_bytes[31:3]};
                        stream_words_remaining <= {3'd0, read_length_bytes[31:3]};
                        beats_unarrived <= 32'd0;
                        fifo_wr_ptr <= '0;
                        fifo_rd_ptr <= '0;
                        fifo_count <= '0;
                        fifo_space <= (FIFO_BITS + 1)'(FIFO_DEPTH);
                    end
                end
            end else if ((stream_words_remaining == 32'd0) && (beats_unarrived == 32'd0)) begin
                // finish only when every issued beat has landed: exiting
                // with responses in flight would poison the next job
                running <= 1'b0;
                busy <= 1'b0;
                done <= 1'b1;
                error <= read_error;
                error_code <= read_error ? ERR_AXI_READ : 8'd0;
                dbg_final_fifo_count <= {{(31 - FIFO_BITS) {1'b0}}, fifo_count};
            end
        end
    end

    task automatic reset_state;
        begin
            running <= 1'b0;
            busy <= 1'b0;
            done <= 1'b0;
            error <= 1'b0;
            error_code <= 8'd0;
            next_address <= '0;
            ar_words_remaining <= 32'd0;
            stream_words_remaining <= 32'd0;
            beats_unarrived <= 32'd0;
            read_error <= 1'b0;
            fifo_wr_ptr <= '0;
            fifo_rd_ptr <= '0;
            fifo_count <= '0;
            fifo_space <= '0;
            m_axi_araddr <= '0;
            m_axi_arlen <= 8'd0;
            m_axi_arvalid <= 1'b0;
            dbg_first_stream_word <= 64'd0;
            dbg_first_araddr <= 32'd0;
            dbg_beats_while_idle <= 32'd0;
            dbg_final_fifo_count <= 32'd0;
            dbg_first_word_seen <= 1'b0;
            dbg_first_araddr_seen <= 1'b0;
        end
    endtask
endmodule

`default_nettype wire

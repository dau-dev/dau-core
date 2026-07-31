`default_nettype none

// Simulation-only AXI4 RAM slave (DATA_WIDTH-bit data, single outstanding
// transaction per direction) used to bench AXI master job runners before
// they meet the real memory controller. Deterministic stall injection
// (periodic arready/rvalid/wready gaps) plus a parameterized read latency
// approximate DDR behavior; a word-indexed backdoor lets benches load and
// inspect memory without AXI traffic.
module dau_axi_ram_sim #(
    parameter int unsigned ADDR_WIDTH = 32,
    parameter int unsigned MEM_WORDS = 65536,
    parameter int unsigned READ_LATENCY = 4,
    parameter int unsigned DATA_WIDTH = 64  // 64 (the classic bench bus) or a wide read tier
) (
    input  wire logic        clk,
    input  wire logic        rst,

    input  wire logic [ADDR_WIDTH-1:0] s_axi_araddr,
    input  wire logic [7:0]  s_axi_arlen,
    input  wire logic [2:0]  s_axi_arsize,
    input  wire logic [1:0]  s_axi_arburst,
    input  wire logic        s_axi_arvalid,
    output logic             s_axi_arready,
    output logic [DATA_WIDTH-1:0] s_axi_rdata,
    output logic [1:0]       s_axi_rresp,
    output logic             s_axi_rlast,
    output logic             s_axi_rvalid,
    input  wire logic        s_axi_rready,
    input  wire logic [ADDR_WIDTH-1:0] s_axi_awaddr,
    input  wire logic [7:0]  s_axi_awlen,
    input  wire logic [2:0]  s_axi_awsize,
    input  wire logic [1:0]  s_axi_awburst,
    input  wire logic        s_axi_awvalid,
    output logic             s_axi_awready,
    input  wire logic [DATA_WIDTH-1:0] s_axi_wdata,
    input  wire logic [DATA_WIDTH/8-1:0] s_axi_wstrb,
    input  wire logic        s_axi_wlast,
    input  wire logic        s_axi_wvalid,
    output logic             s_axi_wready,
    output logic [1:0]       s_axi_bresp,
    output logic             s_axi_bvalid,
    input  wire logic        s_axi_bready,

    input  wire logic        bd_write,
    input  wire logic [31:0] bd_index,
    input  wire logic [DATA_WIDTH-1:0] bd_wdata,
    output logic [DATA_WIDTH-1:0] bd_rdata
);
    localparam int unsigned INDEX_BITS = $clog2(MEM_WORDS);
    localparam int unsigned BYTE_SHIFT = $clog2(DATA_WIDTH / 8);

    typedef enum logic [1:0] {
        READ_IDLE,
        READ_WAIT,
        READ_DATA
    } read_state_t;

    typedef enum logic [1:0] {
        WRITE_IDLE,
        WRITE_DATA,
        WRITE_RESP
    } write_state_t;

    logic [DATA_WIDTH-1:0] mem [MEM_WORDS];

    read_state_t read_state;
    logic [INDEX_BITS-1:0] read_index;
    logic [7:0] read_beats_left;
    logic [7:0] read_wait;

    // pipelined AR acceptance: real controllers take multiple bursts in
    // flight, and masters with credit-based FIFOs only misbehave (or
    // prove themselves) under that pressure
    localparam int unsigned AR_QUEUE_DEPTH = 8;
    logic [ADDR_WIDTH-1:0] ar_queue_addr [AR_QUEUE_DEPTH];
    logic [7:0] ar_queue_len [AR_QUEUE_DEPTH];
    logic [$clog2(AR_QUEUE_DEPTH)-1:0] ar_queue_wr;
    logic [$clog2(AR_QUEUE_DEPTH)-1:0] ar_queue_rd;
    logic [$clog2(AR_QUEUE_DEPTH):0] ar_queue_count;

    write_state_t write_state;
    logic [INDEX_BITS-1:0] write_index;

    // deterministic stall injection so handshakes are exercised with gaps
    logic [2:0] tick;
    logic stall_read;
    logic stall_write;
    assign stall_read = tick == 3'd5;
    assign stall_write = tick == 3'd2;

    assign s_axi_arready = (ar_queue_count != AR_QUEUE_DEPTH[$clog2(AR_QUEUE_DEPTH):0]) && !stall_read;
    assign s_axi_rvalid = (read_state == READ_DATA) && !stall_read;
    assign s_axi_rdata = mem[read_index];
    assign s_axi_rresp = 2'b00;
    assign s_axi_rlast = read_beats_left == 8'd0;

    assign s_axi_awready = write_state == WRITE_IDLE;
    assign s_axi_wready = (write_state == WRITE_DATA) && !stall_write;
    assign s_axi_bvalid = write_state == WRITE_RESP;
    assign s_axi_bresp = 2'b00;

    assign bd_rdata = mem[bd_index[INDEX_BITS-1:0]];

    always_ff @(posedge clk) begin
        if (rst) begin
            read_state <= READ_IDLE;
            read_index <= '0;
            read_beats_left <= 8'd0;
            read_wait <= 8'd0;
            ar_queue_wr <= '0;
            ar_queue_rd <= '0;
            ar_queue_count <= '0;
            write_state <= WRITE_IDLE;
            write_index <= '0;
            tick <= 3'd0;
        end else begin
            tick <= tick + 3'd1;

            if (s_axi_arvalid && s_axi_arready) begin
                ar_queue_addr[ar_queue_wr] <= s_axi_araddr;
                ar_queue_len[ar_queue_wr] <= s_axi_arlen;
                ar_queue_wr <= ar_queue_wr + 1'b1;
            end
            ar_queue_count <= ar_queue_count
                + {{$clog2(AR_QUEUE_DEPTH) {1'b0}}, s_axi_arvalid && s_axi_arready}
                - {{$clog2(AR_QUEUE_DEPTH) {1'b0}},
                   ((read_state == READ_IDLE) && (ar_queue_count != '0))
                   || ((read_state == READ_DATA) && s_axi_rvalid && s_axi_rready && (read_beats_left == 8'd0) && (ar_queue_count != '0))};

            case (read_state)
                READ_IDLE: begin
                    if (ar_queue_count != '0) begin
                        read_index <= ar_queue_addr[ar_queue_rd][INDEX_BITS+BYTE_SHIFT-1:BYTE_SHIFT];
                        read_beats_left <= ar_queue_len[ar_queue_rd];
                        ar_queue_rd <= ar_queue_rd + 1'b1;
                        read_wait <= 8'(READ_LATENCY);
                        read_state <= READ_WAIT;
                    end
                end
                READ_WAIT: begin
                    if (read_wait == 8'd0) begin
                        read_state <= READ_DATA;
                    end else begin
                        read_wait <= read_wait - 8'd1;
                    end
                end
                READ_DATA: begin
                    if (s_axi_rvalid && s_axi_rready) begin
                        read_index <= read_index + 1'b1;
                        if (read_beats_left == 8'd0) begin
                            // stream queued bursts back-to-back: real
                            // controllers deliver continuous rvalid across
                            // burst boundaries, the fill regime where
                            // credit FIFOs must prove themselves
                            if (ar_queue_count != '0) begin
                                read_index <= ar_queue_addr[ar_queue_rd][INDEX_BITS+BYTE_SHIFT-1:BYTE_SHIFT];
                                read_beats_left <= ar_queue_len[ar_queue_rd];
                                ar_queue_rd <= ar_queue_rd + 1'b1;
                            end else begin
                                read_state <= READ_IDLE;
                            end
                        end else begin
                            read_beats_left <= read_beats_left - 8'd1;
                        end
                    end
                end
                default: begin
                    read_state <= READ_IDLE;
                end
            endcase

            case (write_state)
                WRITE_IDLE: begin
                    if (s_axi_awvalid && s_axi_awready) begin
                        write_index <= s_axi_awaddr[INDEX_BITS+BYTE_SHIFT-1:BYTE_SHIFT];
                        write_state <= WRITE_DATA;
                    end
                end
                WRITE_DATA: begin
                    if (s_axi_wvalid && s_axi_wready) begin
                        for (int b = 0; b < DATA_WIDTH / 8; b++) begin
                            if (s_axi_wstrb[b]) begin
                                mem[write_index][8 * b+:8] <= s_axi_wdata[8*b+:8];
                            end
                        end
                        write_index <= write_index + 1'b1;
                        if (s_axi_wlast) begin
                            write_state <= WRITE_RESP;
                        end
                    end
                end
                WRITE_RESP: begin
                    if (s_axi_bready) begin
                        write_state <= WRITE_IDLE;
                    end
                end
                default: begin
                    write_state <= WRITE_IDLE;
                end
            endcase

            if (bd_write) begin
                mem[bd_index[INDEX_BITS-1:0]] <= bd_wdata;
            end
        end
    end
endmodule

`default_nettype wire

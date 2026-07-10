`default_nettype none

// N:1 AXI4 write-channel mux: round-robin grant taken at AW and held
// through the W beats until the write response returns, so bursts from
// different writers never interleave. Sized for record writers whose
// bursts are sparse — one transaction in flight at a time.
module dau_axi_write_mux #(
    parameter int unsigned NUM_INPUTS = 4,
    parameter int unsigned ADDR_WIDTH = 32
) (
    input  wire logic        clk,
    input  wire logic        rst,

    input  wire logic [NUM_INPUTS-1:0][ADDR_WIDTH-1:0] s_awaddr,
    input  wire logic [NUM_INPUTS-1:0][7:0] s_awlen,
    input  wire logic [NUM_INPUTS-1:0] s_awvalid,
    output logic [NUM_INPUTS-1:0] s_awready,
    input  wire logic [NUM_INPUTS-1:0][63:0] s_wdata,
    input  wire logic [NUM_INPUTS-1:0] s_wlast,
    input  wire logic [NUM_INPUTS-1:0] s_wvalid,
    output logic [NUM_INPUTS-1:0] s_wready,
    output logic [1:0]       s_bresp,
    output logic [NUM_INPUTS-1:0] s_bvalid,
    input  wire logic [NUM_INPUTS-1:0] s_bready,

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
    output logic             m_axi_bready
);
    localparam int unsigned GRANT_BITS = (NUM_INPUTS > 1) ? $clog2(NUM_INPUTS) : 1;

    typedef enum logic [1:0] {
        MUX_IDLE,
        MUX_AW,
        MUX_W,
        MUX_B
    } mux_state_t;

    mux_state_t state;
    logic [GRANT_BITS-1:0] grant;
    logic [GRANT_BITS-1:0] next_grant;
    logic next_found;

    assign m_axi_awsize = 3'b011;   // 8 bytes
    assign m_axi_awburst = 2'b01;   // INCR
    assign m_axi_wstrb = 8'hFF;

    assign m_axi_awaddr = s_awaddr[grant];
    assign m_axi_awlen = s_awlen[grant];
    assign m_axi_awvalid = (state == MUX_AW) && s_awvalid[grant];
    assign m_axi_wdata = s_wdata[grant];
    assign m_axi_wlast = s_wlast[grant];
    assign m_axi_wvalid = (state == MUX_W) && s_wvalid[grant];
    assign m_axi_bready = (state == MUX_B) && s_bready[grant];
    assign s_bresp = m_axi_bresp;

    always_comb begin
        for (int unsigned index = 0; index < NUM_INPUTS; index++) begin
            s_awready[index] = (state == MUX_AW) && (grant == GRANT_BITS'(index)) && m_axi_awready;
            s_wready[index] = (state == MUX_W) && (grant == GRANT_BITS'(index)) && m_axi_wready;
            s_bvalid[index] = (state == MUX_B) && (grant == GRANT_BITS'(index)) && m_axi_bvalid;
        end
    end

    // round-robin: first requester at or after the slot past the last grant
    always_comb begin
        next_grant = grant;
        next_found = 1'b0;
        for (int unsigned step = 1; step <= NUM_INPUTS; step++) begin
            automatic logic [GRANT_BITS-1:0] candidate;
            candidate = GRANT_BITS'((32'(grant) + step) % NUM_INPUTS);
            if (!next_found && s_awvalid[candidate]) begin
                next_grant = candidate;
                next_found = 1'b1;
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= MUX_IDLE;
            grant <= '0;
        end else begin
            case (state)
                MUX_IDLE: begin
                    if (next_found) begin
                        grant <= next_grant;
                        state <= MUX_AW;
                    end
                end
                MUX_AW: begin
                    if (m_axi_awvalid && m_axi_awready) begin
                        state <= MUX_W;
                    end
                end
                MUX_W: begin
                    if (m_axi_wvalid && m_axi_wready && m_axi_wlast) begin
                        state <= MUX_B;
                    end
                end
                MUX_B: begin
                    if (m_axi_bvalid && m_axi_bready) begin
                        state <= MUX_IDLE;
                    end
                end
                default: begin
                    state <= MUX_IDLE;
                end
            endcase
        end
    end
endmodule

`default_nettype wire

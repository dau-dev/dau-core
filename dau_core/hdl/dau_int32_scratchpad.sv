`default_nettype none

// Scratchpad owned-arena tile: the deliberately trivial consumer of the OWNED
// storage class (STORAGE_AND_NOC.md phase 1). A private word-addressed BRAM
// arena behind a runtime GRANT; each row either writes a word or reads one,
// and emits the post-op value. Matches dau_core.operators.scratchpad_scan
// bit-for-bit. The contract under test is the grant, not the arithmetic: an
// address at or past cfg_granted_words closes the batch out with
// STORAGE_OVERFLOW — the defined error a grow-request replaces when the
// allocator hardens. The grant arrives on a config port the host writes
// today and an allocator tile drives tomorrow; the tile cannot tell.
//
// Two 64-bit words per row (a row is atomic; input_last only legal on w1):
//   w0 = {value[31:0], addr[31:0]}
//   w1 = {63'b?, we}         (bit 0: 1 = write value at addr, 0 = read addr)
// Output: one 64-bit word per row, {post_op_value[31:0], addr[31:0]}.
//
// The grant is latched on the first beat of each batch; a grant of zero or
// beyond CAPACITY is BAD_GRANT (the batch drains, error close-out). The
// arena persists across batches (grants persist; only the host's allocator
// retires them). Addresses compare unsigned against the grant, so a negative
// int32 address is never inside any grant.
module dau_int32_scratchpad #(
    parameter int CAPACITY = 1024  // arena words; [1, 16384]
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [31:0] cfg_granted_words,
    input  wire logic        input_valid,
    output logic             input_ready,
    input  wire logic [63:0] input_data,
    input  wire logic        input_last,
    output logic             output_valid,
    input  wire logic        output_ready,
    output logic [63:0]      output_data,
    output logic             output_last,
    output logic             status_valid,
    input  wire logic        status_ready,
    output logic             status_error,
    output logic [7:0]       status_error_code
);
    localparam logic [7:0] ERR_BAD_GRANT = 8'd1;
    localparam logic [7:0] ERR_STORAGE_OVERFLOW = 8'd2;
    localparam logic [7:0] ERR_STREAM = 8'd3;
    localparam int AW = (CAPACITY <= 1) ? 1 : $clog2(CAPACITY);

    if (CAPACITY < 1 || CAPACITY > 16384) begin : g_param_check
        $error("CAPACITY must be in [1, 16384]");
    end

    typedef enum logic [2:0] {
        STATE_W0,       // accept {value, addr}; latch the grant on a batch's first beat
        STATE_W1,       // accept {we}; bounds-check against the grant
        STATE_RD,       // one-cycle BRAM access (write-first on we)
        STATE_EMIT,     // present {result, addr}
        STATE_DISCARD,  // error latched: consume the batch to input_last
        STATE_STATUS    // hold the close-out until accepted
    } state_t;

    state_t state;

    (* ram_style = "block" *) logic [31:0] arena [0:CAPACITY-1];

    logic        batch_open;
    logic [31:0] granted;
    logic [31:0] row_addr;
    logic [31:0] row_value;
    logic        row_we;
    logic        row_last;
    logic [31:0] read_data;
    logic        err_pending;
    logic [7:0]  err_code;

    assign input_ready = (state == STATE_W0) || (state == STATE_W1) || (state == STATE_DISCARD);

    // unsigned compare: grant <= 16384, so any address with the sign bit set
    // (or any high bit past the grant) is out of bounds
    logic addr_in_grant;
    assign addr_in_grant = ({1'b0, row_addr} < {1'b0, granted});

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= STATE_W0;
            batch_open <= 1'b0;
            granted <= 32'd0;
            output_valid <= 1'b0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            err_pending <= 1'b0;
            err_code <= 8'd0;
            row_addr <= 32'd0;
            row_value <= 32'd0;
            row_we <= 1'b0;
            row_last <= 1'b0;
            read_data <= 32'd0;
        end else begin
            case (state)
                STATE_W0: begin
                    if (input_valid) begin
                        row_addr <= input_data[31:0];
                        row_value <= input_data[63:32];
                        if (!batch_open) begin
                            batch_open <= 1'b1;
                            granted <= cfg_granted_words;
                            if (cfg_granted_words == 32'd0 || cfg_granted_words > CAPACITY[31:0]) begin
                                err_pending <= 1'b1;
                                err_code <= ERR_BAD_GRANT;
                                state <= input_last ? STATE_STATUS : STATE_DISCARD;
                            end else if (input_last) begin
                                // a row torn at w0 is a stream error
                                err_pending <= 1'b1;
                                err_code <= ERR_STREAM;
                                state <= STATE_STATUS;
                            end else begin
                                state <= STATE_W1;
                            end
                        end else if (input_last) begin
                            err_pending <= 1'b1;
                            err_code <= ERR_STREAM;
                            state <= STATE_STATUS;
                        end else begin
                            state <= STATE_W1;
                        end
                    end
                end
                STATE_W1: begin
                    if (input_valid) begin
                        row_we <= input_data[0];
                        row_last <= input_last;
                        if (!addr_in_grant) begin
                            err_pending <= 1'b1;
                            err_code <= ERR_STORAGE_OVERFLOW;
                            state <= input_last ? STATE_STATUS : STATE_DISCARD;
                        end else begin
                            state <= STATE_RD;
                        end
                    end
                end
                STATE_RD: begin
                    // write-first: a write's post-op value is what it wrote
                    if (row_we) begin
                        arena[row_addr[AW-1:0]] <= row_value;
                        read_data <= row_value;
                    end else begin
                        read_data <= arena[row_addr[AW-1:0]];
                    end
                    state <= STATE_EMIT;
                end
                STATE_EMIT: begin
                    output_valid <= 1'b1;
                    output_data <= {read_data, row_addr};
                    output_last <= row_last;
                    if (output_valid && output_ready) begin
                        output_valid <= 1'b0;
                        output_last <= 1'b0;
                        if (row_last) begin
                            status_valid <= 1'b1;
                            status_error <= 1'b0;
                            status_error_code <= 8'd0;
                            state <= STATE_STATUS;
                        end else begin
                            state <= STATE_W0;
                        end
                    end
                end
                STATE_DISCARD: begin
                    if (input_valid && input_last) begin
                        state <= STATE_STATUS;
                    end
                end
                STATE_STATUS: begin
                    if (err_pending && !status_valid) begin
                        status_valid <= 1'b1;
                        status_error <= 1'b1;
                        status_error_code <= err_code;
                    end
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        err_pending <= 1'b0;
                        batch_open <= 1'b0;
                        state <= STATE_W0;
                    end
                end
                default: state <= STATE_W0;
            endcase
        end
    end
endmodule

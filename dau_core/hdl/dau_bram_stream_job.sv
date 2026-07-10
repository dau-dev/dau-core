`default_nettype none

// BRAM-staged stream job runner (reusable primitive, tile-agnostic).
//
// The host stages an encoded record batch in the input BRAM (via XDMA MM
// writes), sets the input length, and pulses start. This runner streams the
// batch out of BRAM into an attached stream tile, writes the tile's result
// stream into the output BRAM, and reports done/error plus the result length.
// The tile is attached externally through the tile_* stream ports (any module
// speaking the standard DAU 64-bit valid/ready/last stream + status contract);
// binding is done by the dau-build generated shell top. One job at a time; a
// new start clears prior status.
module dau_bram_stream_job #(
    parameter int unsigned INPUT_ADDR_WIDTH = 14,  // 64-bit words: 16384 -> 128 KiB
    parameter int unsigned OUTPUT_ADDR_WIDTH = 9,  // 64-bit words: 512 -> 4 KiB
    parameter int unsigned LENGTH_ALIGN_BITS = 6    // input length grid: 64-byte record-batch grid by default; 3 for 8-byte pair rows
) (
    input  wire logic        clk,
    input  wire logic        rst,

    input  wire logic        start,
    input  wire logic [31:0] input_length_bytes,
    output logic             busy,
    output logic             done,
    output logic             error,
    output logic [7:0]       error_code,
    output logic [31:0]      result_length_bytes,

    // input BRAM read port (1-cycle read latency)
    output logic [INPUT_ADDR_WIDTH-1:0]  in_addr,
    output logic                          in_en,
    input  wire logic [63:0]              in_dout,

    // output BRAM write port
    output logic [OUTPUT_ADDR_WIDTH-1:0] out_addr,
    output logic                          out_en,
    output logic                          out_we,
    output logic [63:0]                   out_din,

    // attached stream tile
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

    typedef enum logic [2:0] {
        JOB_IDLE,
        JOB_FETCH,
        JOB_LOAD,
        JOB_PRESENT,
        JOB_WAIT_STATUS
    } job_state_t;

    job_state_t job_state;

    logic [31:0] words_total;
    logic [31:0] word_index;
    logic [63:0] hold_word;
    logic [OUTPUT_ADDR_WIDTH-1:0] out_index;

    assign tile_input_data = hold_word;
    assign tile_input_last = tile_input_valid && (word_index == words_total - 32'd1);
    assign tile_output_ready = 1'b1;
    assign tile_status_ready = 1'b1;

    // completion is detected via the status channel; tile_output_last is part
    // of the standard stream contract but not needed by this runner
    logic unused_tile_output_last;
    assign unused_tile_output_last = tile_output_last;

    // combinational BRAM read controls: address sampled at the end of
    // JOB_FETCH, data captured during JOB_LOAD (1-cycle read latency)
    assign in_en = (job_state == JOB_FETCH);
    assign in_addr = word_index[INPUT_ADDR_WIDTH-1:0];

    always_ff @(posedge clk) begin
        if (rst) begin
            job_state <= JOB_IDLE;
            busy <= 1'b0;
            done <= 1'b0;
            error <= 1'b0;
            error_code <= 8'd0;
            result_length_bytes <= 32'd0;
            words_total <= 32'd0;
            word_index <= 32'd0;
            hold_word <= 64'd0;
            tile_input_valid <= 1'b0;
            out_addr <= '0;
            out_en <= 1'b0;
            out_we <= 1'b0;
            out_din <= 64'd0;
            out_index <= '0;
        end else begin
            out_en <= 1'b0;
            out_we <= 1'b0;

            // capture the tile's result stream into the output BRAM
            if (tile_output_valid) begin
                out_addr <= out_index;
                out_din <= tile_output_data;
                out_en <= 1'b1;
                out_we <= 1'b1;
                out_index <= out_index + 1'b1;
            end

            unique case (job_state)
                JOB_IDLE: begin
                    if (start) begin
                        done <= 1'b0;
                        error <= 1'b0;
                        error_code <= 8'd0;
                        result_length_bytes <= 32'd0;
                        out_index <= '0;
                        word_index <= 32'd0;
                        if ((input_length_bytes == 32'd0)
                                || (input_length_bytes[LENGTH_ALIGN_BITS-1:0] != '0)
                                || ({3'd0, input_length_bytes[31:3]} > (32'd1 << INPUT_ADDR_WIDTH))) begin
                            done <= 1'b1;
                            error <= 1'b1;
                            error_code <= ERR_JOB_LENGTH;
                        end else begin
                            words_total <= {3'd0, input_length_bytes[31:3]};
                            busy <= 1'b1;
                            job_state <= JOB_FETCH;
                        end
                    end
                end
                JOB_FETCH: begin
                    // in_en/in_addr are combinational off this state
                    job_state <= JOB_LOAD;
                end
                JOB_LOAD: begin
                    // BRAM dout is valid this cycle for the address issued last cycle
                    hold_word <= in_dout;
                    tile_input_valid <= 1'b1;
                    job_state <= JOB_PRESENT;
                end
                JOB_PRESENT: begin
                    if (tile_input_ready) begin
                        tile_input_valid <= 1'b0;
                        if (word_index == words_total - 32'd1) begin
                            job_state <= JOB_WAIT_STATUS;
                        end else begin
                            word_index <= word_index + 32'd1;
                            job_state <= JOB_FETCH;
                        end
                    end
                end
                JOB_WAIT_STATUS: begin
                    if (tile_status_valid) begin
                        busy <= 1'b0;
                        done <= 1'b1;
                        error <= tile_status_error;
                        error_code <= tile_status_error_code;
                        result_length_bytes <= tile_status_error ? 32'd0 : ({{(32 - OUTPUT_ADDR_WIDTH) {1'b0}}, out_index} << 3);
                        job_state <= JOB_IDLE;
                    end
                end
                default: job_state <= JOB_IDLE;
            endcase
        end
    end
endmodule

`default_nettype wire

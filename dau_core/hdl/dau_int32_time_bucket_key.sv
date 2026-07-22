`default_nettype none

// Generic time-bucket key tile: rewrites a row's partition key into a composite
// (partition, time-bucket) group key so a downstream key-grouped aggregator (the
// bar / grouped-sum tiles) produces fixed-width TIME-WINDOWED groups per
// partition. This is workload-agnostic — any windowed aggregation (finance time
// bars, TSBS rate/window shapes, ...) composes it before the generic aggregator;
// the aggregation itself (OHLC, sums, ...) and any per-row derivation stay in
// their own tiles.
//
// bucket = time >>> cfg_bucket_shift (window width = 2**cfg_bucket_shift), and
// the emitted key is {partition[PART_BITS-1:0], bucket[BUCKET_BITS-1:0]} so it
// changes exactly when the partition or the bucket changes (the stream is
// pre-sorted by (partition, time), so buckets are contiguous per partition).
// The downstream recovers partition = key >>> BUCKET_BITS. Contract (a host
// encoding-audit precondition, like the fixed-point-fit rules): the partition id
// fits PART_BITS = 32 - BUCKET_BITS bits and the (host-rebased) bucket fits
// BUCKET_BITS, so the two never collide.
//
// A mid-lane transform: silent on success (the row count is 1:1, output_last
// rides the last row; the terminal aggregator closes the lane out), status only
// on a torn row. The payload words pass through opaquely.
//
// Input row (3 words, atomic; input_last only legal on w2):
//   w0 = {payload_hi[31:0], partition[31:0]}
//   w1 = time[63:0]                            (signed INT64)
//   w2 = {payload3[31:0], payload2[31:0]}
// Output row (2 words — the aggregator's quad-row input):
//   o0 = {payload_hi[31:0], key[31:0]}
//   o1 = {payload3[31:0], payload2[31:0]}
module dau_int32_time_bucket_key #(
    parameter int BUCKET_BITS = 16
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [5:0]  cfg_bucket_shift,   // window width = 2**shift
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
    localparam int PART_BITS = 32 - BUCKET_BITS;
    localparam logic [7:0] ERR_STREAM = 8'd2;
    localparam logic [7:0] ERR_RANGE = 8'd3;

    if (BUCKET_BITS < 1 || BUCKET_BITS > 31) begin : g_param_check
        $error("BUCKET_BITS must be in [1, 31]");
    end

    typedef enum logic [2:0] {
        STATE_W0,
        STATE_W1,
        STATE_W2,
        STATE_COMPUTE,
        STATE_EMIT_O0,
        STATE_EMIT_O1,
        STATE_STATUS
    } state_t;

    state_t state;

    logic signed [31:0] row_partition;
    logic [31:0]        row_payload_hi;
    logic signed [63:0] row_time;
    logic [31:0]        row_payload2;
    logic [31:0]        row_payload3;
    logic               row_last;
    logic [31:0]        row_key;   // the composite (partition, bucket) key

    logic               cfg_latched;   // the bucket shift is latched at batch start
    logic [5:0]         shift_reg;

    logic input_fire;
    logic output_fire;
    logic status_fire;

    logic signed [63:0] bucket;
    logic               part_overflow;   // partition exceeds PART_BITS -> would alias
    logic               bucket_overflow; // bucket exceeds BUCKET_BITS -> would alias

    assign input_ready = ((state == STATE_W0) || (state == STATE_W1) || (state == STATE_W2))
        && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    // bucket = time >>> shift (latched for the batch); the key packs the
    // partition above the bucket. A partition or bucket that does not fit its
    // allotted width would alias two distinct groups into one key, so it is an
    // enforced range error rather than a silent truncation.
    assign bucket = row_time >>> shift_reg;
    assign part_overflow = |row_partition[31:PART_BITS];
    assign bucket_overflow = |bucket[63:BUCKET_BITS];

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_W0: begin
                    if (input_fire) begin
                        if (!cfg_latched) begin
                            shift_reg <= cfg_bucket_shift;  // latch at batch start
                            cfg_latched <= 1'b1;
                        end
                        if (input_last) begin
                            start_error_status(ERR_STREAM);  // last on w0: a torn row
                        end else begin
                            row_partition <= signed'(input_data[31:0]);
                            row_payload_hi <= input_data[63:32];
                            state <= STATE_W1;
                        end
                    end
                end
                STATE_W1: begin
                    if (input_fire) begin
                        if (input_last) begin
                            start_error_status(ERR_STREAM);
                        end else begin
                            row_time <= signed'(input_data);
                            state <= STATE_W2;
                        end
                    end
                end
                STATE_W2: begin
                    if (input_fire) begin
                        row_payload2 <= input_data[31:0];
                        row_payload3 <= input_data[63:32];
                        row_last <= input_last;
                        state <= STATE_COMPUTE;
                    end
                end
                STATE_COMPUTE: begin
                    if (part_overflow || bucket_overflow) begin
                        start_error_status(ERR_RANGE);  // would alias two groups — refuse
                    end else begin
                        // {partition[PART_BITS-1:0], bucket[BUCKET_BITS-1:0]}
                        row_key <= {row_partition[PART_BITS-1:0], bucket[BUCKET_BITS-1:0]};
                        state <= STATE_EMIT_O0;
                    end
                end
                STATE_EMIT_O0: begin
                    if (!output_valid || output_ready) emit_word({row_payload_hi, row_key}, 1'b0, STATE_EMIT_O1);
                end
                STATE_EMIT_O1: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= {row_payload3, row_payload2};
                        output_last <= row_last;   // silent on success; last rides the row
                        state <= row_last ? STATE_STATUS : STATE_W0;
                    end
                end
                STATE_STATUS: begin
                    if (status_valid) begin
                        // error close-out: hold the status until it is accepted
                        if (status_fire) reset_stream_state();
                    end else begin
                        // silent success: drain the last output beat, then idle
                        if (!output_valid) reset_stream_state();
                    end
                end
                default: reset_state();
            endcase
        end
    end

    task automatic emit_word(input logic [63:0] value, input logic last_out, input state_t next_state);
        begin
            output_valid <= 1'b1;
            output_data <= value;
            output_last <= last_out;
            state <= next_state;
        end
    endtask

    task automatic start_error_status(input logic [7:0] code);
        begin
            output_valid <= 1'b0;
            output_last <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_stream_state;
        begin
            state <= STATE_W0;
            cfg_latched <= 1'b0;
            output_valid <= 1'b0;
            output_data <= 64'd0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask

    task automatic reset_state;
        begin
            reset_stream_state();
            shift_reg <= 6'd0;
            row_partition <= 32'sd0;
            row_payload_hi <= 32'd0;
            row_time <= 64'sd0;
            row_payload2 <= 32'd0;
            row_payload3 <= 32'd0;
            row_last <= 1'b0;
            row_key <= 32'd0;
        end
    endtask
endmodule

`default_nettype wire

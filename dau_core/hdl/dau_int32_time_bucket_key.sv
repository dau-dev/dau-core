`default_nettype none

`include "types.svh"

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
// fits PART_BITS = KEY_BITS - BUCKET_BITS bits and the (host-rebased) bucket
// fits BUCKET_BITS, so the two never collide.
//
// A mid-lane transform: silent on success (the row count is 1:1, output_last
// rides the last row; the terminal aggregator closes the lane out), status only
// on a torn row. The payload words pass through opaquely.
//
// Input row (3 words, atomic; input_last only legal on w2), two KEY_WIDTH
// lanes tiling each word except the time axis:
//   w0 = {payload_hi, partition}
//   w1 = time[63:0]                            (signed INT64)
//   w2 = {payload3, payload2}
// Output row (2 words — the aggregator's quad-row input):
//   o0 = {payload_hi, key}
//   o1 = {payload3, payload2}
module dau_int32_time_bucket_key #(
    parameter int BUCKET_BITS = 16,
    // The ELEMENT TYPE of the partition id, the composite key it becomes and
    // the payload lanes riding beside them, as encoded by types.svh. Two such
    // lanes tile one 64-bit stream word, so a narrower type packs the row
    // into less of the word rather than changing the beat. A plain signed
    // integer encodes as its own width, so `DAU_TYPE_INT32 is 32 and this
    // builds exactly the key the tile has always built.
    //
    // THE TIME AXIS IS NOT THIS ELEMENT. It is a signed INT64 in its own
    // word, because nanosecond timestamps need one, and it stays 64 bits at
    // every key width. cfg_bucket_shift shifts that axis and is a shift
    // amount, not an element, so it keeps its own 6 bits.
    parameter int KEY_WIDTH = `DAU_TYPE_INT32
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
    // the type, taken apart: the key and payload lanes size off KEY_BITS and
    // the float fact is what the guard checks the type against
    localparam int KEY_BITS     = `DAU_TYPE_WIDTH(KEY_WIDTH);
    localparam int KEY_IS_FLOAT = `DAU_TYPE_IS_FLOAT(KEY_WIDTH);
    localparam int PART_BITS = KEY_BITS - BUCKET_BITS;
    localparam logic [7:0] ERR_STREAM = 8'd2;
    localparam logic [7:0] ERR_RANGE = 8'd3;

    // the registry offers int32 only, where the key is 32 bits and this is
    // the whole bound; the relation guard below is what holds at any other
    // width the RTL elaborates
    if (BUCKET_BITS < 1 || BUCKET_BITS > 31) begin : g_param_check
        $error("BUCKET_BITS must be in [1, 31]");
    end
    // {payload, key} tile one 64-bit stream word, so the pair must fit it and
    // divide it evenly
    if (KEY_BITS < 8 || KEY_BITS > 32 || (64 % KEY_BITS) != 0) begin : g_key_width_check
        $error("KEY_WIDTH must divide 64 and lie in [8, 32]");
    end
    // the bucket rides BELOW the partition in one key, so the partition needs
    // what is left; a bucket filling the key would leave no partition bits
    // and alias every partition onto one group
    if (BUCKET_BITS >= KEY_BITS) begin : g_bucket_fits_key
        $error("BUCKET_BITS must not fill the whole key: the partition needs KEY_WIDTH - BUCKET_BITS bits above the bucket");
    end
    // A TYPE IS NOT A CAPABILITY. The key is a GROUP IDENTITY built by
    // concatenation and taken apart by a shift: a float's exponent bits would
    // become partition bits and its mantissa bucket bits, and the overflow
    // checks would read a magnitude that is not there. Refuse it at
    // elaboration.
    if (KEY_IS_FLOAT != 0) begin : g_key_float_check
        $error("KEY_WIDTH must not name a floating-point type: the key is a packed group identity, not a magnitude");
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

    logic signed [KEY_BITS-1:0] row_partition;
    logic [KEY_BITS-1:0]        row_payload_hi;
    logic signed [63:0] row_time;
    logic [KEY_BITS-1:0]        row_payload2;
    logic [KEY_BITS-1:0]        row_payload3;
    logic               row_last;
    logic [KEY_BITS-1:0]        row_key;   // the composite (partition, bucket) key

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
    assign part_overflow = |row_partition[KEY_BITS-1:PART_BITS];
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
                            row_partition <= signed'(input_data[KEY_BITS-1:0]);
                            row_payload_hi <= input_data[KEY_BITS +: KEY_BITS];
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
                        row_payload2 <= input_data[KEY_BITS-1:0];
                        row_payload3 <= input_data[KEY_BITS +: KEY_BITS];
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
                    // the record occupies the low lanes of the stream word;
                    // a lane narrower than half the word leaves the rest zero
                    if (!output_valid || output_ready) emit_word(64'({row_payload_hi, row_key}), 1'b0, STATE_EMIT_O1);
                end
                STATE_EMIT_O1: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= 64'({row_payload3, row_payload2});
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
            row_partition <= '0;
            row_payload_hi <= '0;
            row_time <= 64'sd0;
            row_payload2 <= '0;
            row_payload3 <= '0;
            row_last <= 1'b0;
            row_key <= '0;
        end
    endtask
endmodule

`default_nettype wire

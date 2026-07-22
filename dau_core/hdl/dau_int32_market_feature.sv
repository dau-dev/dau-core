`default_nettype none

// Market-feature map tile (the `derive` stage of the temporal workflow): a
// stateless per-row map from an aligned market row to its derived features —
// midpoint, spread, quote-rule trade sign, signed volume, and dollar volume
// (the VWAP numerator). One output record per input row; matches
// dau_core.temporal.market_features bit-for-bit. In the fused datapath this
// sits between the as-of tile and the bar tile (a chain stage); its signed
// volume is the value the bar tile accumulates.
//
// Input: the as-of market row, four 64-bit words (a row is atomic; input_last
// is only legal on r3):
//   r0 = time[63:0]                 (signed INT64)
//   r1 = {price[31:0], symbol[31:0]}
//   r2 = {ask[31:0], bid[31:0]}
//   r3 = {32'd0, size[31:0]}
//
// Output: a six-word feature record:
//   o0 = time[63:0]
//   o1 = {price[31:0], symbol[31:0]}
//   o2 = {trade_sign[31:0], midpoint[31:0]}   midpoint = (bid+ask)>>1; sign +1/-1/0
//   o3 = {32'd0, size[31:0]}
//   o4 = spread[63:0]                          ask - bid (INT64; the int32 range can overflow)
//   o5 = signed_volume[63:0]                   trade_sign * size (INT64)
//   o6 = dollar_volume[63:0]                   price * size (INT64)
//
// A row torn by input_last before its fourth word is ERR_STREAM.
module dau_int32_market_feature (
    input  wire logic        clk,
    input  wire logic        rst,
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
    output logic [7:0]       status_error_code,
    output logic [63:0]      feature_count
);
    localparam logic [7:0] ERR_STREAM = 8'd2;

    typedef enum logic [3:0] {
        STATE_R0,
        STATE_R1,
        STATE_R2,
        STATE_R3,
        STATE_COMPUTE,
        STATE_EMIT_O0,
        STATE_EMIT_O1,
        STATE_EMIT_O2,
        STATE_EMIT_O3,
        STATE_EMIT_O4,
        STATE_EMIT_O5,
        STATE_EMIT_O6,
        STATE_STATUS
    } state_t;

    state_t state;

    // latched input row fields
    logic signed [63:0] row_time;
    logic signed [31:0] row_symbol;
    logic signed [31:0] row_price;
    logic signed [31:0] row_bid;
    logic signed [31:0] row_ask;
    logic signed [31:0] row_size;
    logic               row_last;

    // registered derived features (the COMPUTE beat keeps the 32x32 dollar
    // product off the emit path)
    logic signed [31:0] f_midpoint;
    logic signed [63:0] f_spread;   // ask-bid: two int32 quotes can exceed int32
    logic signed [31:0] f_trade_sign;
    logic signed [63:0] f_signed_volume;
    logic signed [63:0] f_dollar_volume;

    logic input_fire;
    logic output_fire;
    logic status_fire;

    logic signed [31:0] beat_lo;
    logic signed [31:0] beat_hi;

    // COMPUTE-beat combinational features from the latched row
    logic signed [32:0] mid_sum;   // 33-bit: bid+ask can exceed int32
    logic signed [31:0] midpoint;
    logic signed [31:0] trade_sign;

    assign input_ready = ((state == STATE_R0) || (state == STATE_R1) || (state == STATE_R2) || (state == STATE_R3))
        && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    assign beat_lo = signed'(input_data[31:0]);
    assign beat_hi = signed'(input_data[63:32]);

    // midpoint = floor((bid+ask)/2): the sum needs 33 bits (two int32 prices can
    // exceed int32), then an arithmetic right shift floors toward -inf, matching
    // Python's (bid+ask)//2
    assign mid_sum = {row_bid[31], row_bid} + {row_ask[31], row_ask};
    assign midpoint = mid_sum >>> 1;
    assign trade_sign = (row_price > midpoint) ? 32'sd1 : (row_price < midpoint) ? -32'sd1 : 32'sd0;

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_R0: begin
                    if (input_fire) begin
                        if (input_last) begin
                            start_error_status(ERR_STREAM);  // last on r0: a torn row
                        end else begin
                            row_time <= signed'(input_data);
                            state <= STATE_R1;
                        end
                    end
                end
                STATE_R1: begin
                    if (input_fire) begin
                        if (input_last) begin
                            start_error_status(ERR_STREAM);
                        end else begin
                            row_symbol <= beat_lo;
                            row_price <= beat_hi;
                            state <= STATE_R2;
                        end
                    end
                end
                STATE_R2: begin
                    if (input_fire) begin
                        if (input_last) begin
                            start_error_status(ERR_STREAM);
                        end else begin
                            row_bid <= beat_lo;
                            row_ask <= beat_hi;
                            state <= STATE_R3;
                        end
                    end
                end
                STATE_R3: begin
                    if (input_fire) begin
                        row_size <= beat_lo;
                        row_last <= input_last;
                        state <= STATE_COMPUTE;
                    end
                end
                STATE_COMPUTE: begin
                    f_midpoint <= midpoint;
                    f_spread <= 64'(row_ask) - 64'(row_bid);  // 33-bit range, held in int64
                    f_trade_sign <= trade_sign;
                    // signed volume = trade_sign * size, without a multiplier
                    f_signed_volume <= (trade_sign == 32'sd1) ? 64'(row_size)
                        : (trade_sign == -32'sd1) ? -64'(row_size) : 64'sd0;
                    f_dollar_volume <= row_price * row_size;  // 32x32 signed -> 64-bit, registered
                    state <= STATE_EMIT_O0;
                end
                STATE_EMIT_O0: begin
                    if (!output_valid || output_ready) emit_word(unsigned'(row_time), 1'b0, STATE_EMIT_O1);
                end
                STATE_EMIT_O1: begin
                    if (!output_valid || output_ready) emit_word({row_price, row_symbol}, 1'b0, STATE_EMIT_O2);
                end
                STATE_EMIT_O2: begin
                    if (!output_valid || output_ready) emit_word({f_trade_sign, f_midpoint}, 1'b0, STATE_EMIT_O3);
                end
                STATE_EMIT_O3: begin
                    if (!output_valid || output_ready) emit_word({32'd0, row_size}, 1'b0, STATE_EMIT_O4);
                end
                STATE_EMIT_O4: begin
                    if (!output_valid || output_ready) emit_word(unsigned'(f_spread), 1'b0, STATE_EMIT_O5);
                end
                STATE_EMIT_O5: begin
                    if (!output_valid || output_ready) emit_word(unsigned'(f_signed_volume), 1'b0, STATE_EMIT_O6);
                end
                STATE_EMIT_O6: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= unsigned'(f_dollar_volume);
                        output_last <= row_last;
                        feature_count <= feature_count + 64'd1;
                        state <= row_last ? STATE_STATUS : STATE_R0;
                    end
                end
                STATE_STATUS: begin
                    if (!status_valid) begin
                        if (!output_valid || output_ready) begin
                            status_valid <= 1'b1;
                            status_error <= 1'b0;
                            status_error_code <= 8'd0;
                        end
                    end else if (status_fire) begin
                        reset_stream_state();
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
            state <= STATE_R0;
            feature_count <= 64'd0;
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
            row_time <= 64'sd0;
            row_symbol <= 32'sd0;
            row_price <= 32'sd0;
            row_bid <= 32'sd0;
            row_ask <= 32'sd0;
            row_size <= 32'sd0;
            row_last <= 1'b0;
            f_midpoint <= 32'sd0;
            f_spread <= 64'sd0;
            f_trade_sign <= 32'sd0;
            f_signed_volume <= 64'sd0;
            f_dollar_volume <= 64'sd0;
        end
    endtask
endmodule

`default_nettype wire

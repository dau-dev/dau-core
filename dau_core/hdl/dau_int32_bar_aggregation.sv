`default_nettype none

// Fused single-pass bar-aggregation tile (ordered close-out): price OHLC
// statistics plus the micro-share volume decomposition in one pass.
//
// Consumes two 64-bit words per row:
//   w0 = {price[31:0], key[31:0]}
//   w1 = {frac[31:0], shares[31:0]}
// (all signed INT32; a row is atomic — input_last is only legal on a w1).
// A bar closes when the key changes or the stream ends; close-out emits a
// 6-word record:
//   r0 = {open, key}
//   r1 = {low, close}
//   r2 = {32'd0, high}
//   r3 = shares_sum[63:0]   (INT64)
//   r4 = frac_sum[63:0]     (INT64)
//   r5 = count[63:0]
//
// Reappearing keys open new partial bars (the host merges — the
// partial-bar contract). cfg_row_count = N checks the stream length in
// rows; 0 selects last-driven mode. Configuration latches at the first
// beat; matches dau_core.operators.bar_aggregate_rows bit-for-bit.
module dau_int32_bar_aggregation (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [1:0]  cfg_mode,
    input  wire logic [63:0] cfg_row_count,
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
    output logic [63:0]      bar_count
);
    localparam logic [1:0] MODE_ORDERED = 2'd0;

    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;

    typedef enum logic [3:0] {
        STATE_IDLE,
        STATE_ROW_LOW,
        STATE_ROW_HIGH,
        STATE_PENDING_HIGH,
        STATE_EMIT_R0,
        STATE_EMIT_R1,
        STATE_EMIT_R2,
        STATE_EMIT_R3,
        STATE_EMIT_R4,
        STATE_EMIT_R5,
        STATE_DRAIN_ERROR,
        STATE_STATUS_PEND,
        STATE_STATUS
    } state_t;

    state_t state;

    logic        count_checked;
    logic [63:0] rows_remaining;
    logic        bar_open;
    logic signed [31:0] bar_key;
    logic signed [31:0] bar_price_open;
    logic signed [31:0] bar_price_close;
    logic signed [31:0] bar_price_low;
    logic signed [31:0] bar_price_high;
    logic signed [63:0] bar_shares;
    logic signed [63:0] bar_frac;
    logic [63:0] bar_rows;
    logic        final_after_emit;
    logic signed [31:0] held_key;
    logic signed [31:0] held_price;
    logic        held_is_pending;
    logic [7:0]  pending_error_code;

    logic input_fire;
    logic output_fire;
    logic status_fire;
    logic config_ok;
    logic final_row;
    logic key_change;

    logic [63:0] eff_remaining;
    logic        eff_count_checked;
    logic signed [31:0] row_key;
    logic signed [31:0] row_price;
    logic signed [31:0] row_shares;
    logic signed [31:0] row_frac;

    // the row counter loads at the first beat (w0) and decrements at each
    // w1 — final_row is only consulted in the w1 states, so no IDLE mux
    assign eff_remaining = rows_remaining;
    assign eff_count_checked = count_checked;

    assign input_ready = ((state == STATE_IDLE) || (state == STATE_ROW_LOW) || (state == STATE_ROW_HIGH)
        || (state == STATE_PENDING_HIGH) || (state == STATE_DRAIN_ERROR))
        && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    assign config_ok = cfg_mode == MODE_ORDERED;

    // one ROW per two words: the down-counter decrements at w1
    assign final_row = eff_count_checked ? ((~|eff_remaining[63:1]) && eff_remaining[0]) : input_last;

    assign row_key = signed'(input_data[31:0]);
    assign row_price = signed'(input_data[63:32]);
    assign row_shares = signed'(input_data[31:0]);
    assign row_frac = signed'(input_data[63:32]);
    assign key_change = bar_open && (row_key != bar_key);

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_IDLE, STATE_ROW_LOW: begin
                    if (input_fire) begin
                        if ((state == STATE_IDLE) && !config_ok) begin
                            fail(ERR_CONFIG);
                        end else if (input_last) begin
                            // a row is two words: last on w0 truncates the row
                            start_error_status(ERR_STREAM);
                        end else begin
                            if (state == STATE_IDLE) begin
                                count_checked <= |cfg_row_count;
                                rows_remaining <= cfg_row_count;
                            end
                            held_key <= row_key;
                            held_price <= row_price;
                            if (key_change) begin
                                // close the live bar; this row opens the next
                                // bar once its w1 arrives after the emit
                                held_is_pending <= 1'b1;
                                final_after_emit <= 1'b0;
                                state <= STATE_EMIT_R0;
                            end else begin
                                state <= STATE_ROW_HIGH;
                            end
                        end
                    end
                end
                STATE_ROW_HIGH, STATE_PENDING_HIGH: begin
                    if (input_fire) begin
                        if (eff_count_checked && (input_last != final_row)) begin
                            if (input_last) begin
                                start_error_status(ERR_STREAM);
                            end else begin
                                fail(ERR_STREAM);
                            end
                        end else begin
                            rows_remaining <= eff_remaining - 64'd1;
                            if ((state == STATE_PENDING_HIGH) || !bar_open) begin
                                open_bar(held_key, held_price, row_shares, row_frac);
                            end else begin
                                absorb_row(held_price, row_shares, row_frac);
                            end
                            if (final_row) begin
                                final_after_emit <= 1'b1;
                                state <= STATE_EMIT_R0;
                            end else begin
                                state <= STATE_ROW_LOW;
                            end
                        end
                    end
                end
                STATE_EMIT_R0: begin
                    if (!output_valid || output_ready) begin
                        emit_word({bar_price_open, bar_key}, 1'b0, STATE_EMIT_R1);
                    end
                end
                STATE_EMIT_R1: begin
                    if (!output_valid || output_ready) begin
                        emit_word({bar_price_low, bar_price_close}, 1'b0, STATE_EMIT_R2);
                    end
                end
                STATE_EMIT_R2: begin
                    if (!output_valid || output_ready) begin
                        emit_word({32'd0, bar_price_high}, 1'b0, STATE_EMIT_R3);
                    end
                end
                STATE_EMIT_R3: begin
                    if (!output_valid || output_ready) begin
                        emit_word(unsigned'(bar_shares), 1'b0, STATE_EMIT_R4);
                    end
                end
                STATE_EMIT_R4: begin
                    if (!output_valid || output_ready) begin
                        emit_word(unsigned'(bar_frac), 1'b0, STATE_EMIT_R5);
                    end
                end
                STATE_EMIT_R5: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= bar_rows;
                        output_last <= final_after_emit && !held_is_pending;
                        bar_count <= bar_count + 64'd1;
                        bar_open <= 1'b0;
                        if (held_is_pending) begin
                            held_is_pending <= 1'b0;
                            state <= STATE_PENDING_HIGH;
                        end else begin
                            state <= STATE_STATUS_PEND;
                        end
                    end
                end
                STATE_DRAIN_ERROR: begin
                    if (input_fire && input_last) begin
                        start_error_status(pending_error_code);
                    end
                end
                STATE_STATUS_PEND: begin
                    if (!output_valid || output_ready) begin
                        status_valid <= 1'b1;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= STATE_STATUS;
                    end
                end
                STATE_STATUS: begin
                    if (status_fire) begin
                        reset_stream_state();
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    task automatic emit_word(input logic [63:0] value, input logic last_word, input state_t next_state);
        begin
            output_valid <= 1'b1;
            output_data <= value;
            output_last <= last_word;
            state <= next_state;
        end
    endtask

    task automatic open_bar(
        input logic signed [31:0] key,
        input logic signed [31:0] price,
        input logic signed [31:0] shares,
        input logic signed [31:0] frac
    );
        begin
            bar_open <= 1'b1;
            bar_key <= key;
            bar_price_open <= price;
            bar_price_close <= price;
            bar_price_low <= price;
            bar_price_high <= price;
            bar_shares <= 64'(shares);
            bar_frac <= 64'(frac);
            bar_rows <= 64'd1;
        end
    endtask

    task automatic absorb_row(
        input logic signed [31:0] price,
        input logic signed [31:0] shares,
        input logic signed [31:0] frac
    );
        begin
            bar_price_close <= price;
            if (price < bar_price_low) begin
                bar_price_low <= price;
            end
            if (price > bar_price_high) begin
                bar_price_high <= price;
            end
            bar_shares <= bar_shares + 64'(shares);
            bar_frac <= bar_frac + 64'(frac);
            bar_rows <= bar_rows + 64'd1;
        end
    endtask

    task automatic fail(input logic [7:0] code);
        begin
            pending_error_code <= code;
            if (input_last) begin
                start_error_status(code);
            end else begin
                state <= STATE_DRAIN_ERROR;
            end
        end
    endtask

    task automatic start_error_status(input logic [7:0] code);
        begin
            output_valid <= 1'b0;
            output_last <= 1'b0;
            held_is_pending <= 1'b0;
            bar_open <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_stream_state;
        begin
            state <= STATE_IDLE;
            count_checked <= 1'b0;
            rows_remaining <= 64'd0;
            bar_open <= 1'b0;
            final_after_emit <= 1'b0;
            held_is_pending <= 1'b0;
            pending_error_code <= 8'd0;
            bar_count <= 64'd0;
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
            bar_key <= 32'd0;
            bar_price_open <= 32'd0;
            bar_price_close <= 32'd0;
            bar_price_low <= 32'd0;
            bar_price_high <= 32'd0;
            bar_shares <= 64'd0;
            bar_frac <= 64'd0;
            bar_rows <= 64'd0;
            held_key <= 32'd0;
            held_price <= 32'd0;
        end
    endtask
endmodule

`default_nettype wire

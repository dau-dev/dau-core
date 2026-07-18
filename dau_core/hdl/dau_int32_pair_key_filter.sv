`default_nettype none

// Row-atomic key partition filter for 16-byte bar rows (two 64-bit words,
// w0 = {price, key}): rows pass when (key & cfg_key_mask) == cfg_key_match,
// and whole rows pass or drop together. The newest passing row is held in
// a skid slot and released when a newer passing row completes (last=0) or
// when the stream ends (last=1) — so input_last always lands on the final
// forwarded word even when trailing rows drop.
//
// The filter raises its own status only for the two cases the downstream
// tile cannot see: a stream that ends with zero forwarded rows closes out
// with a success status (the tile never started), and a torn final row
// (input_last on a w0) reports ERR_STREAM. Otherwise the tile's status is
// authoritative and the filter stays silent.
module dau_int32_pair_key_filter (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [31:0] cfg_key_mask,
    input  wire logic [31:0] cfg_key_match,
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
    localparam logic [7:0] ERR_STREAM = 8'd2;

    typedef enum logic [2:0] {
        STATE_W0,
        STATE_W1,
        STATE_FLUSH,
        STATE_FLUSH_DRAIN,  // hold input closed until the flushed final beat drains, then re-arm
        STATE_STATUS
    } state_t;

    typedef enum logic [1:0] {
        EMIT_IDLE,
        EMIT_W0,
        EMIT_W1
    } emit_t;

    state_t state;
    emit_t emit_state;

    logic [63:0] row_w0;
    logic        row_keep;
    logic        held_valid;
    logic [63:0] held_w0;
    logic [63:0] held_w1;
    logic [63:0] emit_w0;
    logic [63:0] emit_w1;
    logic        emit_last;

    logic input_fire;
    logic output_fire;
    logic keep_now;
    logic emit_free;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign keep_now = (input_data[31:0] & cfg_key_mask) == cfg_key_match;
    // the emit engine frees on the accepted final beat of a row
    assign emit_free = (emit_state == EMIT_IDLE) || ((emit_state == EMIT_W1) && output_fire);

    // accepting w1 of a passing row displaces the held row into the emit
    // engine, so it must be free (or freeing) that cycle
    assign input_ready = (state == STATE_W0) || ((state == STATE_W1) && (!row_keep || !held_valid || emit_free));

    assign output_valid = emit_state != EMIT_IDLE;
    assign output_data = (emit_state == EMIT_W0) ? emit_w0 : emit_w1;
    assign output_last = (emit_state == EMIT_W1) && emit_last;

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            case (emit_state)
                EMIT_W0: begin
                    if (output_fire) begin
                        emit_state <= EMIT_W1;
                    end
                end
                EMIT_W1: begin
                    if (output_fire) begin
                        emit_state <= EMIT_IDLE;
                    end
                end
                default: ;
            endcase

            case (state)
                STATE_W0: begin
                    if (input_fire) begin
                        if (input_last) begin
                            // torn row: the batch is invalid regardless of
                            // what was already forwarded
                            held_valid <= 1'b0;
                            status_valid <= 1'b1;
                            status_error <= 1'b1;
                            status_error_code <= ERR_STREAM;
                            state <= STATE_STATUS;
                        end else begin
                            row_w0 <= input_data;
                            row_keep <= keep_now;
                            state <= STATE_W1;
                        end
                    end
                end
                STATE_W1: begin
                    if (input_fire) begin
                        if (row_keep) begin
                            if (held_valid) begin
                                start_emit(held_w0, held_w1, 1'b0);
                            end
                            held_valid <= 1'b1;
                            held_w0 <= row_w0;
                            held_w1 <= input_data;
                        end
                        if (input_last) begin
                            if (row_keep || held_valid) begin
                                state <= STATE_FLUSH;
                            end else begin
                                // nothing forwarded: close out for the tile
                                status_valid <= 1'b1;
                                status_error <= 1'b0;
                                status_error_code <= 8'd0;
                                state <= STATE_STATUS;
                            end
                        end else begin
                            state <= STATE_W0;
                        end
                    end
                end
                STATE_FLUSH: begin
                    // release the held final row tagged last; do NOT re-arm yet
                    // (input stays closed via STATE_FLUSH_DRAIN) so a following
                    // batch's close-out cannot overtake this still-in-flight tail
                    if (emit_free) begin
                        start_emit(held_w0, held_w1, 1'b1);
                        held_valid <= 1'b0;
                        state <= STATE_FLUSH_DRAIN;
                    end
                end
                STATE_FLUSH_DRAIN: begin
                    // re-arm only once the flushed final beat has drained from
                    // the emit engine (silent on success)
                    if (emit_state == EMIT_IDLE) begin
                        state <= STATE_W0;
                    end
                end
                STATE_STATUS: begin
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= STATE_W0;
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    task automatic start_emit(input logic [63:0] w0, input logic [63:0] w1, input logic last_row);
        begin
            emit_w0 <= w0;
            emit_w1 <= w1;
            emit_last <= last_row;
            emit_state <= EMIT_W0;
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_W0;
            emit_state <= EMIT_IDLE;
            row_w0 <= 64'd0;
            row_keep <= 1'b0;
            held_valid <= 1'b0;
            held_w0 <= 64'd0;
            held_w1 <= 64'd0;
            emit_w0 <= 64'd0;
            emit_w1 <= 64'd0;
            emit_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask
endmodule

`default_nettype wire

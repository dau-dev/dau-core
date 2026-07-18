`default_nettype none

// Row-atomic compound predicate filter for 16-byte quad rows (two 64-bit
// words, w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th 32-bit
// lane in (key, value, aux0, aux1) order). Rows pass when the two-level
// predicate tree (term0 <combine01> term1) <combine2> term2 matches — each
// term compares one selected field against a signed 32-bit constant with a
// dau_predicate_pkg opcode, and SINGLE at a combine level ignores the
// right-hand term (the 2-term filter's combine, extended to three terms).
// Whole rows pass or drop together; the newest passing row is held in a
// skid slot and released when a newer passing row completes or the stream
// ends, so input_last always lands on the final forwarded word even when
// trailing rows drop (dau_core.operators.filter_rows_by_predicate is the
// golden). This is the q6/q19 WHERE stage.
//
// The filter raises its own status only for what downstream tiles cannot
// see: a stream that ends with zero forwarded rows closes out with a
// success status, a torn final row (input_last on a w0) reports ERR_STREAM,
// and an invalid predicate config (bad opcode/combine, checked at the first
// row of a batch and latched for the batch) drains the batch and reports
// ERR_CONFIG. Otherwise the downstream tile's status is authoritative and
// the filter stays silent. selected_count reports the rows forwarded by the
// current/last batch (0 after an error close-out).
module dau_int32_row_predicate_filter
    import dau_predicate_pkg::*;
(
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [1:0]  cfg_field0,
    input  wire logic [2:0]  cfg_op0,
    input  wire logic [31:0] cfg_constant0,
    input  wire logic [1:0]  cfg_combine01,
    input  wire logic [1:0]  cfg_field1,
    input  wire logic [2:0]  cfg_op1,
    input  wire logic [31:0] cfg_constant1,
    input  wire logic [1:0]  cfg_combine2,
    input  wire logic [1:0]  cfg_field2,
    input  wire logic [2:0]  cfg_op2,
    input  wire logic [31:0] cfg_constant2,
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
    output logic [63:0]      selected_count
);
    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;

    typedef enum logic [2:0] {
        STATE_W0,      // accept w0 of a row (batch config latches on the first)
        STATE_W1,      // accept w1 of a row, evaluate the predicate
        STATE_FLUSH,   // release the held final row with last set
        STATE_FLUSH_DRAIN, // hold input closed until the flushed final beat drains, then re-arm
        STATE_DISCARD, // bad config: consume the batch to input_last
        STATE_STATUS   // hold a close-out (success or error) until accepted
    } state_t;

    typedef enum logic [1:0] {
        EMIT_IDLE,
        EMIT_W0,
        EMIT_W1
    } emit_t;

    state_t state;
    emit_t emit_state;

    // batch-latched predicate config (quasi-static cfg inputs sampled at the
    // first row so a mid-batch change cannot tear the predicate)
    logic        batch_open;
    logic [1:0]  active_field0;
    logic [2:0]  active_op0;
    logic [31:0] active_constant0;
    logic [1:0]  active_combine01;
    logic [1:0]  active_field1;
    logic [2:0]  active_op1;
    logic [31:0] active_constant1;
    logic [1:0]  active_combine2;
    logic [1:0]  active_field2;
    logic [2:0]  active_op2;
    logic [31:0] active_constant2;

    logic [63:0] row_w0;
    logic        held_valid;
    logic [63:0] held_w0;
    logic [63:0] held_w1;
    logic [63:0] emit_w0;
    logic [63:0] emit_w1;
    logic        emit_last;

    logic input_fire;
    logic output_fire;
    logic emit_free;
    logic config_ok;
    logic first_row;
    logic row_match;

    logic [1:0]  eff_field0;
    logic [2:0]  eff_op0;
    logic [31:0] eff_constant0;
    logic [1:0]  eff_combine01;
    logic [1:0]  eff_field1;
    logic [2:0]  eff_op1;
    logic [31:0] eff_constant1;
    logic [1:0]  eff_combine2;
    logic [1:0]  eff_field2;
    logic [2:0]  eff_op2;
    logic [31:0] eff_constant2;

    logic term0;
    logic term1;
    logic term2;
    logic level01;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    // the emit engine frees on the accepted final beat of a row
    assign emit_free = (emit_state == EMIT_IDLE) || ((emit_state == EMIT_W1) && output_fire);

    // accepting w1 may displace the held row into the emit engine, so it
    // must be free (or freeing) that cycle; the check is match-independent
    // so ready never depends on the incoming data
    assign input_ready = (state == STATE_W0) || (state == STATE_DISCARD) || ((state == STATE_W1) && (!held_valid || emit_free));

    assign output_valid = emit_state != EMIT_IDLE;
    assign output_data = (emit_state == EMIT_W0) ? emit_w0 : emit_w1;
    assign output_last = (emit_state == EMIT_W1) && emit_last;

    assign first_row = !batch_open;

    assign eff_field0 = first_row ? cfg_field0 : active_field0;
    assign eff_op0 = first_row ? cfg_op0 : active_op0;
    assign eff_constant0 = first_row ? cfg_constant0 : active_constant0;
    assign eff_combine01 = first_row ? cfg_combine01 : active_combine01;
    assign eff_field1 = first_row ? cfg_field1 : active_field1;
    assign eff_op1 = first_row ? cfg_op1 : active_op1;
    assign eff_constant1 = first_row ? cfg_constant1 : active_constant1;
    assign eff_combine2 = first_row ? cfg_combine2 : active_combine2;
    assign eff_field2 = first_row ? cfg_field2 : active_field2;
    assign eff_op2 = first_row ? cfg_op2 : active_op2;
    assign eff_constant2 = first_row ? cfg_constant2 : active_constant2;

    assign config_ok = predicate_op_valid(cfg_op0)
        && (cfg_combine01 <= COMBINE_OR)
        && (cfg_combine2 <= COMBINE_OR)
        && ((cfg_combine01 == COMBINE_SINGLE) || predicate_op_valid(cfg_op1))
        && ((cfg_combine2 == COMBINE_SINGLE) || predicate_op_valid(cfg_op2));

    // the four fields of the row being completed: w0 was registered, w1 is
    // on the input this cycle
    function automatic logic [31:0] field_select(input logic [1:0] sel, input logic [63:0] w0, input logic [63:0] w1);
        begin
            unique case (sel)
                2'd0: field_select = w0[31:0];
                2'd1: field_select = w0[63:32];
                2'd2: field_select = w1[31:0];
                default: field_select = w1[63:32];
            endcase
        end
    endfunction

    assign term0 = predicate_compare(eff_op0, field_select(eff_field0, row_w0, input_data), eff_constant0);
    assign term1 = predicate_compare(eff_op1, field_select(eff_field1, row_w0, input_data), eff_constant1);
    assign term2 = predicate_compare(eff_op2, field_select(eff_field2, row_w0, input_data), eff_constant2);
    assign level01 = (eff_combine01 == COMBINE_AND) ? (term0 && term1)
        : (eff_combine01 == COMBINE_OR) ? (term0 || term1)
        : term0;
    assign row_match = (eff_combine2 == COMBINE_AND) ? (level01 && term2)
        : (eff_combine2 == COMBINE_OR) ? (level01 || term2)
        : level01;

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
                        if (first_row) begin
                            batch_open <= 1'b1;
                            selected_count <= 64'd0;
                            active_field0 <= cfg_field0;
                            active_op0 <= cfg_op0;
                            active_constant0 <= cfg_constant0;
                            active_combine01 <= cfg_combine01;
                            active_field1 <= cfg_field1;
                            active_op1 <= cfg_op1;
                            active_constant1 <= cfg_constant1;
                            active_combine2 <= cfg_combine2;
                            active_field2 <= cfg_field2;
                            active_op2 <= cfg_op2;
                            active_constant2 <= cfg_constant2;
                        end
                        if (first_row && !config_ok) begin
                            // config precedence over stream shape (the keyed
                            // filter's rule): drain the batch, then CONFIG
                            if (input_last) begin
                                raise_error(ERR_CONFIG);
                            end else begin
                                held_valid <= 1'b0;
                                state <= STATE_DISCARD;
                            end
                        end else if (input_last) begin
                            // torn row: the batch is invalid regardless of
                            // what was already forwarded
                            raise_error(ERR_STREAM);
                        end else begin
                            row_w0 <= input_data;
                            state <= STATE_W1;
                        end
                    end
                end
                STATE_W1: begin
                    if (input_fire) begin
                        if (row_match) begin
                            if (held_valid) begin
                                start_emit(held_w0, held_w1, 1'b0);
                            end
                            held_valid <= 1'b1;
                            held_w0 <= row_w0;
                            held_w1 <= input_data;
                            selected_count <= selected_count + 64'd1;
                        end
                        if (input_last) begin
                            if (row_match || held_valid) begin
                                state <= STATE_FLUSH;
                            end else begin
                                // nothing forwarded: close out for the tile
                                batch_open <= 1'b0;
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
                        batch_open <= 1'b0;
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
                STATE_DISCARD: begin
                    if (input_fire && input_last) begin
                        raise_error(ERR_CONFIG);
                    end
                end
                STATE_STATUS: begin
                    if (status_valid && status_ready) begin
                        status_valid <= 1'b0;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        batch_open <= 1'b0;
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

    task automatic raise_error(input logic [7:0] code);
        begin
            held_valid <= 1'b0;
            selected_count <= 64'd0;
            batch_open <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_W0;
            emit_state <= EMIT_IDLE;
            batch_open <= 1'b0;
            active_field0 <= 2'd0;
            active_op0 <= 3'd0;
            active_constant0 <= 32'd0;
            active_combine01 <= 2'd0;
            active_field1 <= 2'd0;
            active_op1 <= 3'd0;
            active_constant1 <= 32'd0;
            active_combine2 <= 2'd0;
            active_field2 <= 2'd0;
            active_op2 <= 3'd0;
            active_constant2 <= 32'd0;
            row_w0 <= 64'd0;
            held_valid <= 1'b0;
            held_w0 <= 64'd0;
            held_w1 <= 64'd0;
            emit_w0 <= 64'd0;
            emit_w1 <= 64'd0;
            emit_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            selected_count <= 64'd0;
        end
    endtask
endmodule

`default_nettype wire

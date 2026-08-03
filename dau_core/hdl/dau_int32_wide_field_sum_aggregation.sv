`default_nettype none

// Wide-lane batch field-sum aggregation: SLOTS whole 16-byte quad rows per
// beat, one beat per cycle — the multi-row-per-cycle terminal of the wide
// lane (ingest SLOTS rows/cycle vs the 64-bit tile's one per two). A slot
// carries one row {aux1, aux0, value, key} (field i at bits [32*i +: 32]);
// the reserved ALL-ONES 128-bit row is the PAD (the ragged-tail / dropped-
// slot convention shared with the wide dispatcher and the wide filter):
// pads are neither accumulated nor counted, so an upstream wide predicate
// filter drops a row by replacing its slot with the pad — no compaction.
//
// Semantics are dau_core.operators.field_sum_rows, identical to the 64-bit
// tile: one signed 64-bit sum per cfg_field_mask-selected field plus the
// row count, emitted as one record at input_last (sums ascending, then the
// count word with output_last), then the success close-out. The mask
// latches at the first beat; a zero mask drains the batch and reports
// ERR_CONFIG. Torn rows cannot exist (a beat is whole rows), so the 64-bit
// tile's ERR_STREAM path has no equivalent here. An all-pad beat (a
// trailing pad-only tail) accumulates nothing and closes normally.
module dau_int32_wide_field_sum_aggregation #(
    parameter int SLOTS = 4  // quad rows per beat: 1, 2, or 4
) (
    input  wire logic                     clk,
    input  wire logic                     rst,
    input  wire logic [3:0]               cfg_field_mask,
    input  wire logic                     input_valid,
    output logic                          input_ready,
    input  wire logic [SLOTS*128-1:0]     input_data,
    input  wire logic                     input_last,
    output logic                          output_valid,
    input  wire logic                     output_ready,
    output logic [63:0]                   output_data,
    output logic                          output_last,
    output logic                          status_valid,
    input  wire logic                     status_ready,
    output logic                          status_error,
    output logic [7:0]                    status_error_code,
    output logic [63:0]                   aggregated_count
);
    if (!(SLOTS == 1 || SLOTS == 2 || SLOTS == 4)) begin : g_bad_slots
        $error("SLOTS must be 1, 2, or 4");
    end

    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [127:0] PAD_ROW = {128{1'b1}};

    typedef enum logic [2:0] {
        STATE_ACCUM,   // accept one beat of SLOTS rows per cycle
        STATE_EMIT,    // walk the selected sums, then the count word
        STATE_CLOSE,   // count word accepted -> raise the success close-out
        STATE_DISCARD, // bad config: consume the batch to input_last
        STATE_STATUS   // hold a close-out (success or error) until accepted
    } state_t;

    state_t state;

    logic        batch_open;
    logic [3:0]  active_mask;
    logic signed [63:0] sums [4];
    logic [63:0] row_count;
    logic [2:0]  emit_phase;

    logic input_fire;
    logic output_fire;
    logic first_beat;
    logic [3:0] eff_mask;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign input_ready = (state == STATE_ACCUM) || (state == STATE_DISCARD);

    assign first_beat = !batch_open;
    assign eff_mask = first_beat ? cfg_field_mask : active_mask;

    // Per-beat combinational reduction: for each field, a BALANCED adder tree
    // over non-pad slots, plus the non-pad slot count.
    //
    // Two things here are deliberate and were not always true. The tree is
    // balanced -- (a+b)+(c+d), depth ceil(log2(SLOTS)) -- rather than a
    // left-fold accumulation, whose depth is SLOTS. And it is BEAT_SUM_W wide,
    // not 64: summing SLOTS signed 32-bit values needs 32+clog2(SLOTS) bits,
    // and only the running accumulator below needs the full 64. A left-fold at
    // 64 bits put four chained 64-bit carry chains ahead of the accumulator and
    // made this tile the path that bound the whole job clock.
    localparam int BEAT_SUM_W = 32 + $clog2(SLOTS);
    localparam int ROWS_W = $clog2(SLOTS + 1);

    logic [3:0] slot_live;                              // padded to 4; unused slots stay 0
    logic signed [BEAT_SUM_W-1:0] slot_term [0:3][0:3]; // [field][slot], 0 for pads and unused slots
    logic signed [BEAT_SUM_W-1:0] beat_sum [4];
    logic [ROWS_W-1:0] beat_rows;

    always_comb begin
        slot_live = 4'd0;
        for (int f = 0; f < 4; f++) begin
            for (int s = 0; s < 4; s++) begin
                slot_term[f][s] = '0;
            end
        end
        for (int s = 0; s < SLOTS; s++) begin
            slot_live[s] = (input_data[128*s +: 128] != PAD_ROW);
            for (int f = 0; f < 4; f++) begin
                if (slot_live[s]) begin
                    slot_term[f][s] = BEAT_SUM_W'(signed'(input_data[128*s + 32*f +: 32]));
                end
            end
        end
        beat_rows = ROWS_W'($countones(slot_live));
        for (int f = 0; f < 4; f++) begin
            beat_sum[f] = (slot_term[f][0] + slot_term[f][1]) + (slot_term[f][2] + slot_term[f][3]);
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_ACCUM: begin
                    if (input_fire) begin
                        if (first_beat && (cfg_field_mask == 4'd0)) begin
                            if (input_last) begin
                                raise_error(ERR_CONFIG);
                            end else begin
                                batch_open <= 1'b1;
                                state <= STATE_DISCARD;
                            end
                        end else begin
                            if (first_beat) begin
                                batch_open <= 1'b1;
                                active_mask <= cfg_field_mask;
                                for (int f = 0; f < 4; f++) begin
                                    sums[f] <= (eff_mask[f]) ? beat_sum[f] : 64'sd0;
                                end
                                row_count <= beat_rows;
                            end else begin
                                for (int f = 0; f < 4; f++) begin
                                    if (active_mask[f]) begin
                                        sums[f] <= sums[f] + beat_sum[f];
                                    end
                                end
                                row_count <= row_count + beat_rows;
                            end
                            if (input_last) begin
                                emit_phase <= 3'd0;
                                state <= STATE_EMIT;
                            end
                        end
                    end
                end
                STATE_EMIT: begin
                    if (!output_valid || output_ready) begin
                        if (emit_phase < 3'd4) begin
                            if (active_mask[emit_phase[1:0]]) begin
                                output_valid <= 1'b1;
                                output_data <= unsigned'(sums[emit_phase[1:0]]);
                                output_last <= 1'b0;
                            end
                            emit_phase <= emit_phase + 3'd1;
                        end else begin
                            output_valid <= 1'b1;
                            output_data <= row_count;
                            output_last <= 1'b1;
                            state <= STATE_CLOSE;
                        end
                    end
                end
                STATE_CLOSE: begin
                    if (!output_valid || output_ready) begin
                        aggregated_count <= row_count;
                        batch_open <= 1'b0;
                        status_valid <= 1'b1;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                        state <= STATE_STATUS;
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
                        state <= STATE_ACCUM;
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

    task automatic raise_error(input logic [7:0] code);
        begin
            output_valid <= 1'b0;
            output_last <= 1'b0;
            batch_open <= 1'b0;
            aggregated_count <= 64'd0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_ACCUM;
            batch_open <= 1'b0;
            active_mask <= 4'd0;
            for (int f = 0; f < 4; f++) begin
                sums[f] <= 64'sd0;
            end
            row_count <= 64'd0;
            emit_phase <= 3'd0;
            output_valid <= 1'b0;
            output_data <= 64'd0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            aggregated_count <= 64'd0;
        end
    endtask
endmodule

`default_nettype wire

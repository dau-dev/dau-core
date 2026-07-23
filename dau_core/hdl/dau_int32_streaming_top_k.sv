`default_nettype none

// Streaming top-k tile (ORDER BY <key> DESC LIMIT K): over quad rows
// (key, v0, v1, v2) keeps the K rows with the largest signed-INT32 sort key
// (field 0), breaking ties by arrival order (earliest wins), and at close-out
// drains them sorted key-descending (ties arrival-ascending). This is the
// q3/q10 and TSBS group-by-order-by-limit shape. Matches
// dau_core.operators.streaming_top_k bit-for-bit.
//
// Two 64-bit words per row (a row is atomic; input_last only legal on w1):
//   w0 = {v0[31:0], key[31:0]}
//   w1 = {v2[31:0], v1[31:0]}
// The retained set lives in a size-K systolic sorted register array: cell 0 is
// the largest kept key, cell K-1 the smallest (the eviction target). On each
// row every cell makes a LOCAL compare-and-shift decision — constant critical
// path regardless of K, one row every two cycles. Physical position encodes
// arrival order, so a later row with an equal key inserts AFTER the earlier one
// (strict `<` compare) and the earliest ties are retained; no arrival counter
// is stored. A row inserts iff some cell is empty or its key is strictly less
// than the arriving key (a full array with an equal-or-smaller key drops the
// row). Close-out drains cell 0 outward (shift-left), so output is already
// sorted; the count is the number of retained rows (min(K, input rows)).
//
// K is compile-time (like WINDOW / BUCKET_BITS): the host / compiler validates
// K in [1, 128] (dau_core.operators.TOP_K_MAX; a request outside is BAD_K at
// composition time), enforced here by an elaboration guard. A row torn by
// input_last on w0 is ERR_STREAM.
module dau_int32_streaming_top_k #(
    parameter int K = 8
) (
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
    output logic [63:0]      top_count
);
    localparam logic [7:0] ERR_STREAM = 8'd2;
    localparam int CW = $clog2(K + 1);

    if (K < 1 || K > 128) begin : g_param_check
        $error("K must be in [1, 128]");
    end

    typedef enum logic [2:0] {
        STATE_ROW_LOW,
        STATE_ROW_HIGH,
        STATE_DRAIN_INIT,
        STATE_DRAIN_LOW,
        STATE_DRAIN_HIGH,
        STATE_STATUS
    } state_t;

    state_t state;

    logic               cell_valid [K];
    logic signed [31:0] cell_key   [K];
    logic signed [31:0] cell_v0    [K];
    logic signed [31:0] cell_v1    [K];
    logic signed [31:0] cell_v2    [K];
    logic [CW-1:0]      fill;   // number of retained rows (0..K)
    logic [CW-1:0]      rem;    // rows left to drain

    logic signed [31:0] held_key;
    logic signed [31:0] held_v0;

    logic input_fire;
    logic output_fire;
    logic status_fire;

    assign input_ready = ((state == STATE_ROW_LOW) || (state == STATE_ROW_HIGH))
        && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_ROW_LOW: begin
                    if (input_fire) begin
                        if (input_last) begin
                            // a row is two words: last on w0 truncates the row
                            start_error_status(ERR_STREAM);
                        end else begin
                            held_key <= signed'(input_data[31:0]);
                            held_v0  <= signed'(input_data[63:32]);
                            state <= STATE_ROW_HIGH;
                        end
                    end
                end
                STATE_ROW_HIGH: begin
                    if (input_fire) begin
                        insert_row(held_key, held_v0,
                                   signed'(input_data[31:0]), signed'(input_data[63:32]));
                        state <= input_last ? STATE_DRAIN_INIT : STATE_ROW_LOW;
                    end
                end
                STATE_DRAIN_INIT: begin
                    // fill has settled from the final insert; capture the count
                    rem <= fill;
                    top_count <= 64'(fill);
                    state <= (fill == 0) ? STATE_STATUS : STATE_DRAIN_LOW;
                end
                STATE_DRAIN_LOW: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= {cell_v0[0], cell_key[0]};  // w0 = {v0, key}
                        output_last <= 1'b0;
                        state <= STATE_DRAIN_HIGH;
                    end
                end
                STATE_DRAIN_HIGH: begin
                    if (!output_valid || output_ready) begin
                        output_valid <= 1'b1;
                        output_data <= {cell_v2[0], cell_v1[0]};  // w1 = {v2, v1}
                        output_last <= (rem == 1);
                        shift_out();
                        rem <= rem - 1;
                        state <= (rem == 1) ? STATE_STATUS : STATE_DRAIN_LOW;
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

    // Insert a row into the sorted array: each cell keeps its value, takes the
    // arriving row, or takes its left neighbour's old value. qual is monotonic
    // (empties and strictly-smaller keys sit at the tail), so exactly one cell
    // takes the new row and everything from there shifts right, dropping the
    // tail when full.
    task automatic insert_row(input logic signed [31:0] rk, rv0, rv1, rv2);
        logic qual [K];
        begin
            for (int i = 0; i < K; i++)
                qual[i] = (!cell_valid[i]) || (cell_key[i] < rk);
            for (int i = 0; i < K; i++) begin
                if (qual[i] && (i == 0 || !qual[i-1])) begin
                    cell_valid[i] <= 1'b1;
                    cell_key[i]   <= rk;
                    cell_v0[i]    <= rv0;
                    cell_v1[i]    <= rv1;
                    cell_v2[i]    <= rv2;
                end else if (qual[i]) begin  // i > 0 && qual[i-1]: shift right
                    cell_valid[i] <= cell_valid[i-1];
                    cell_key[i]   <= cell_key[i-1];
                    cell_v0[i]    <= cell_v0[i-1];
                    cell_v1[i]    <= cell_v1[i-1];
                    cell_v2[i]    <= cell_v2[i-1];
                end
                // else: keep (no assignment)
            end
            if (fill < K[CW-1:0]) fill <= fill + 1'b1;
        end
    endtask

    task automatic shift_out;
        begin
            for (int i = 0; i < K - 1; i++) begin
                cell_valid[i] <= cell_valid[i+1];
                cell_key[i]   <= cell_key[i+1];
                cell_v0[i]    <= cell_v0[i+1];
                cell_v1[i]    <= cell_v1[i+1];
                cell_v2[i]    <= cell_v2[i+1];
            end
            cell_valid[K-1] <= 1'b0;
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
            state <= STATE_ROW_LOW;
            fill <= '0;
            rem <= '0;
            top_count <= 64'd0;
            output_valid <= 1'b0;
            output_data <= 64'd0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            for (int i = 0; i < K; i++) cell_valid[i] <= 1'b0;
        end
    endtask

    task automatic reset_state;
        begin
            reset_stream_state();
            held_key <= 32'sd0;
            held_v0 <= 32'sd0;
            for (int i = 0; i < K; i++) begin
                cell_key[i] <= 32'sd0;
                cell_v0[i] <= 32'sd0;
                cell_v1[i] <= 32'sd0;
                cell_v2[i] <= 32'sd0;
            end
        end
    endtask
endmodule

`default_nettype wire

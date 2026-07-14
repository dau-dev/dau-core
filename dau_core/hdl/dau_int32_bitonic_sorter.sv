`default_nettype none

// Batch bitonic sorter for 16-byte pair rows (two 64-bit words,
// w0 = {value, key}). Loads up to CAPACITY whole rows, sorts them ascending
// by the signed 32-bit key with an iterative bitonic network (one
// compare-exchange stage per cycle, log2(C)*(log2(C)+1)/2 stages), then
// drains the batch in key order. The sort is made STABLE by tie-breaking
// equal keys on load order — rows with equal keys drain in input order, so
// the sorter can serve as the local-sort stage of a partition -> sort ->
// concat pipeline without disturbing partition stability.
//
// This is the Q100 Sorter shape: a bounded batch sorter that must be fed
// partitions no larger than CAPACITY. A batch that overflows CAPACITY rows
// is consumed to its end and reported as ERR_CAPACITY (partition first —
// the HARP recipe); the batch's rows are dropped. input_last on a w0 is a
// torn row and reports ERR_STREAM — torn detection takes precedence over
// overflow (a lone last-carrying w0 after CAPACITY rows is ERR_STREAM, the
// universal rule across cores, not ERR_CAPACITY). A legal batch of n rows
// emits exactly n rows with input_last landing on the final word, then a
// success close-out status (the tile convention: the record writer's done
// waits on a consumed producer status). Error close-outs report through
// the same status stream.
module dau_int32_bitonic_sorter #(
    parameter int CAPACITY = 16  // power of two >= 2
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
    // rows drained by the last completed batch (the tile's trailing status
    // counter for the NoC lane register block); 0 after an error close-out
    output logic [63:0]      sorted_count
);
    localparam logic [7:0] ERR_STREAM = 8'd2;
    localparam logic [7:0] ERR_CAPACITY = 8'd3;
    localparam int IDXW = $clog2(CAPACITY);

    if (CAPACITY < 2 || (CAPACITY & (CAPACITY - 1)) != 0) begin : g_param_check
        $error("CAPACITY must be a power of two >= 2");
    end

    typedef enum logic [2:0] {
        STATE_W0,      // accept w0 of a row
        STATE_W1,      // accept w1 of a row
        STATE_PAD,     // fill unused entries with +max sentinels
        STATE_SORT,    // one bitonic compare-exchange stage per cycle
        STATE_DRAIN,   // emit sorted rows
        STATE_DISCARD, // overflowed batch: consume to input_last, then error
        STATE_STATUS   // hold a close-out (success or error) until accepted
    } state_t;

    state_t state;

    logic [63:0]     mem_w0 [CAPACITY];
    logic [63:0]     mem_w1 [CAPACITY];
    logic [IDXW-1:0] mem_idx [CAPACITY];

    logic [IDXW:0] count;        // rows loaded (0..CAPACITY)
    logic [63:0]   row_w0;       // pending w0 of the row in flight
    logic [IDXW:0] k_size;       // bitonic subsequence size (2..CAPACITY)
    logic [IDXW:0] j_dist;       // compare distance within the stage
    logic [IDXW:0] drain_index;
    logic          drain_phase;  // 0 = w0, 1 = w1

    logic input_fire;
    logic output_fire;

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;

    assign input_ready = (state == STATE_W0) || (state == STATE_W1) || (state == STATE_DISCARD);

    assign output_valid = state == STATE_DRAIN;
    assign output_data = drain_phase ? mem_w1[drain_index[IDXW-1:0]] : mem_w0[drain_index[IDXW-1:0]];
    assign output_last = drain_phase && (drain_index == count - 1'b1);

    // ascending (key, load-index) order: index tie-break makes the sort
    // stable and orders the +max pad sentinels after every real row
    function automatic logic entry_gt(input logic [31:0] a_key, input logic [IDXW-1:0] a_idx, input logic [31:0] b_key, input logic [IDXW-1:0] b_idx);
        if ($signed(a_key) != $signed(b_key)) begin
            return $signed(a_key) > $signed(b_key);
        end
        return a_idx > b_idx;
    endfunction

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            case (state)
                STATE_W0: begin
                    if (input_fire) begin
                        if (input_last) begin
                            // torn row: the batch is invalid
                            raise_status(ERR_STREAM);
                        end else if (count == CAPACITY[IDXW:0]) begin
                            // no room for another row: consume to the end of
                            // the batch, then report the overflow
                            state <= STATE_DISCARD;
                        end else begin
                            row_w0 <= input_data;
                            state <= STATE_W1;
                        end
                    end
                end
                STATE_W1: begin
                    if (input_fire) begin
                        mem_w0[count[IDXW-1:0]] <= row_w0;
                        mem_w1[count[IDXW-1:0]] <= input_data;
                        mem_idx[count[IDXW-1:0]] <= count[IDXW-1:0];
                        count <= count + 1'b1;
                        state <= input_last ? STATE_PAD : STATE_W0;
                    end
                end
                STATE_PAD: begin
                    // unused entries become +max sentinels; the index
                    // tie-break (pad indices >= count) sinks them below every
                    // real row, including real INT32_MAX keys
                    for (int i = 0; i < CAPACITY; i++) begin
                        if (i >= int'(count)) begin
                            mem_w0[i] <= {32'd0, 32'h7FFF_FFFF};
                            mem_idx[i] <= IDXW'(unsigned'(i));
                        end
                    end
                    k_size <= (IDXW + 1)'(2'd2);
                    j_dist <= (IDXW + 1)'(1'b1);
                    state <= STATE_SORT;
                end
                STATE_SORT: begin
                    // one bitonic stage: every index pairs with (index ^ j);
                    // the pairing is an involution, so each entry is written
                    // at most once and non-blocking reads see stage-entry
                    // values. The outer unrolled loop pins each possible j to
                    // a compile-time constant so every partner is a fixed
                    // wire (log2(C) mux candidates per entry) instead of a
                    // runtime i^j crossbar (which synthesizes O(C^2) muxing).
                    for (int jj = 0; jj < IDXW; jj++) begin
                        if (j_dist == (IDXW + 1)'(unsigned'(1 << jj))) begin
                            for (int i = 0; i < CAPACITY; i++) begin
                                automatic int partner = i ^ (1 << jj);
                                if (partner > i) begin
                                    automatic logic ascending = (i & int'(k_size)) == 0;
                                    if (entry_gt(mem_w0[i][31:0], mem_idx[i], mem_w0[partner][31:0], mem_idx[partner]) == ascending) begin
                                        mem_w0[i] <= mem_w0[partner];
                                        mem_w0[partner] <= mem_w0[i];
                                        mem_w1[i] <= mem_w1[partner];
                                        mem_w1[partner] <= mem_w1[i];
                                        mem_idx[i] <= mem_idx[partner];
                                        mem_idx[partner] <= mem_idx[i];
                                    end
                                end
                            end
                        end
                    end
                    if (j_dist != (IDXW + 1)'(1'b1)) begin
                        j_dist <= j_dist >> 1;
                    end else if (k_size != CAPACITY[IDXW:0]) begin
                        k_size <= k_size << 1;
                        j_dist <= k_size;
                    end else begin
                        drain_index <= '0;
                        drain_phase <= 1'b0;
                        state <= STATE_DRAIN;
                    end
                end
                STATE_DRAIN: begin
                    if (output_fire) begin
                        if (!drain_phase) begin
                            drain_phase <= 1'b1;
                        end else begin
                            drain_phase <= 1'b0;
                            if (drain_index == count - 1'b1) begin
                                // success close-out: the downstream unit
                                // (record writer) waits on a consumed status
                                sorted_count <= 64'(count);
                                count <= '0;
                                status_valid <= 1'b1;
                                status_error <= 1'b0;
                                status_error_code <= 8'd0;
                                state <= STATE_STATUS;
                            end else begin
                                drain_index <= drain_index + 1'b1;
                            end
                        end
                    end
                end
                STATE_DISCARD: begin
                    if (input_fire && input_last) begin
                        raise_status(ERR_CAPACITY);
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

    task automatic raise_status(input logic [7:0] code);
        begin
            count <= '0;
            sorted_count <= 64'd0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_W0;
            count <= '0;
            row_w0 <= 64'd0;
            k_size <= '0;
            j_dist <= '0;
            drain_index <= '0;
            drain_phase <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            sorted_count <= 64'd0;
        end
    endtask
endmodule

`default_nettype wire

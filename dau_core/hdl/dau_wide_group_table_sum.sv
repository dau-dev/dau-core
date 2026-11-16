`default_nettype none

// Wide-lane grouped field sums over a direct-indexed group table: SLOTS whole
// 16-byte quad rows per beat, one beat per cycle. Each row's group is a code
// taken from its key, (key >> cfg_group_shift) & (2**GROUP_BITS - 1), and
// every row adds its four int32 fields (sign-extended) and a count into that
// group's 64-bit accumulators. At input_last one record per group that
// received a row is emitted, in ascending group order: the group code, one
// sum per cfg_field_mask-selected field (ascending field), then the count,
// with output_last on the final group's count word; then the success
// close-out. dau_core.operators.group_table_sum_rows is the golden.
//
// Each slot accumulates into its own bank, so a bank sees at most one row
// per cycle and its read-modify-write completes within the cycle (an
// asynchronous distributed-RAM read, the add, a write at the edge): rows of
// one group in consecutive beats cannot collide. The banks are added per
// group at close, and every entry is cleared as it is read, which leaves the
// table empty for the next batch.
//
// Reset leaves the RAM as it was, so after reset the tile sweeps the table
// to zero, one group per cycle, before it accepts a beat: a batch cut off by
// a reset cannot leave its sums behind for the next one.
//
// The reserved ALL-ONES 128-bit row is the pad: neither summed nor counted.
// The mask and shift latch at the first beat; a zero mask drains the batch
// and reports ERR_CONFIG. A batch whose rows are all pads emits no record
// and closes out successfully.
module dau_wide_group_table_sum #(
    parameter int SLOTS = 4,       // quad rows per beat: 1, 2, 4, or 8
    parameter int GROUP_BITS = 6   // the table holds 2**GROUP_BITS groups
) (
    input  wire logic                 clk,
    input  wire logic                 rst,
    input  wire logic [3:0]           cfg_field_mask,
    input  wire logic [4:0]           cfg_group_shift,
    input  wire logic                 input_valid,
    output logic                      input_ready,
    input  wire logic [SLOTS*128-1:0] input_data,
    input  wire logic                 input_last,
    output logic                      output_valid,
    input  wire logic                 output_ready,
    output logic [63:0]               output_data,
    output logic                      output_last,
    output logic                      status_valid,
    input  wire logic                 status_ready,
    output logic                      status_error,
    output logic [7:0]                status_error_code,
    output logic [63:0]               aggregated_count
);
    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam int GROUPS = 1 << GROUP_BITS;
    localparam int WORDS = 5;  // four field sums, then the count

    if (!(SLOTS == 1 || SLOTS == 2 || SLOTS == 4 || SLOTS == 8)) begin : g_bad_slots
        $error("SLOTS must be 1, 2, 4, or 8");
    end
    if (GROUP_BITS < 1 || GROUP_BITS > 10) begin : g_bad_group_bits
        $error("GROUP_BITS must lie in [1, 10]");
    end

    typedef enum logic [3:0] {
        STATE_CLEAR,    // after reset: zero the table, one group per cycle
        STATE_ACCUM,    // accept beats into the table
        STATE_DRAIN,    // the last beat's write lands
        STATE_SETTLE,   // ...and its groups are marked occupied
        STATE_PAIR,     // add the banks in pairs, clear the group's entries
        STATE_TOTAL,    // add the pairs
        STATE_EMIT,     // stream the group's record
        STATE_DISCARD,  // bad config: consume the batch to input_last
        STATE_STATUS    // hold the close-out until accepted
    } state_t;

    state_t state;

    logic [3:0]  active_mask;
    logic [4:0]  active_shift;
    logic        batch_open;

    // the registered beat: one row, its pad flag and group code per slot
    logic                  stage_valid;
    logic [127:0]          stage_row   [SLOTS];
    logic                  stage_pad   [SLOTS];
    logic [GROUP_BITS-1:0] stage_group [SLOTS];

    // bank b, word w (0-3 the field sums, 4 the count), group g
    logic [63:0] table_mem [SLOTS][WORDS][GROUPS];
    logic [GROUPS-1:0] occupied;

    logic [GROUP_BITS-1:0] emit_group;
    logic [GROUP_BITS-1:0] clear_group;
    logic [63:0] pair_sum  [(SLOTS+1)/2][WORDS];
    logic [63:0] total     [WORDS];
    logic [2:0]  emit_word;  // 0 = group code, 1-4 = field sums, 5 = count
    logic        emit_final;

    logic input_fire;
    logic first_beat;
    logic [4:0] eff_shift;

    assign input_fire = input_valid && input_ready;
    assign input_ready = (state == STATE_ACCUM || state == STATE_DISCARD) && !status_valid;
    assign first_beat = !batch_open;
    assign eff_shift = first_beat ? cfg_group_shift : active_shift;

    function automatic logic [63:0] field_extend(input logic [127:0] row, input int field);
        field_extend = 64'(signed'(row[field*32 +: 32]));
    endfunction

    // the highest occupied group below or at `from`'s successor: the next
    // group to emit, or none
    function automatic logic next_occupied(input logic [GROUPS-1:0] bits, input logic [GROUP_BITS-1:0] after, output logic [GROUP_BITS-1:0] found);
        next_occupied = 1'b0;
        found = '0;
        for (int g = GROUPS - 1; g >= 0; g--) begin
            if (bits[g] && g > after) begin
                next_occupied = 1'b1;
                found = GROUP_BITS'(g);
            end
        end
    endfunction

    logic [GROUP_BITS-1:0] first_group;
    logic                  has_group;
    logic [GROUP_BITS-1:0] following_group;
    logic                  has_following;

    always_comb begin
        has_group = 1'b0;
        first_group = '0;
        for (int g = GROUPS - 1; g >= 0; g--) begin
            if (occupied[g]) begin
                has_group = 1'b1;
                first_group = GROUP_BITS'(g);
            end
        end
        has_following = next_occupied(occupied, emit_group, following_group);
    end

    // accumulate the staged beat: each bank's read-modify-write in one cycle
    always_ff @(posedge clk) begin
        if (state == STATE_CLEAR) begin
            for (int b = 0; b < SLOTS; b++) begin
                for (int w = 0; w < WORDS; w++) begin
                    table_mem[b][w][clear_group] <= 64'd0;
                end
            end
        end else if (state == STATE_PAIR) begin
            for (int b = 0; b < SLOTS; b++) begin
                for (int w = 0; w < WORDS; w++) begin
                    table_mem[b][w][emit_group] <= 64'd0;
                end
            end
        end else if (stage_valid) begin
            for (int b = 0; b < SLOTS; b++) begin
                if (!stage_pad[b]) begin
                    for (int w = 0; w < 4; w++) begin
                        table_mem[b][w][stage_group[b]] <= table_mem[b][w][stage_group[b]] + field_extend(stage_row[b], w);
                    end
                    table_mem[b][4][stage_group[b]] <= table_mem[b][4][stage_group[b]] + 64'd1;
                end
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= STATE_CLEAR;
            clear_group <= '0;
            batch_open <= 1'b0;
            active_mask <= 4'd0;
            active_shift <= 5'd0;
            stage_valid <= 1'b0;
            occupied <= '0;
            emit_group <= '0;
            emit_word <= 3'd0;
            emit_final <= 1'b0;
            output_valid <= 1'b0;
            output_data <= 64'd0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            aggregated_count <= 64'd0;
        end else begin
            stage_valid <= 1'b0;
            if (output_valid && output_ready) begin
                output_valid <= 1'b0;
                output_last <= 1'b0;
            end
            case (state)
                STATE_CLEAR: begin
                    clear_group <= clear_group + 1'b1;
                    if (clear_group == GROUP_BITS'(GROUPS - 1)) begin
                        state <= STATE_ACCUM;
                    end
                end
                STATE_ACCUM: begin
                    if (input_fire) begin
                        if (first_beat) begin
                            batch_open <= 1'b1;
                            active_mask <= cfg_field_mask;
                            active_shift <= cfg_group_shift;
                            aggregated_count <= 64'd0;
                        end
                        if (first_beat && cfg_field_mask == 4'd0) begin
                            if (input_last) begin
                                raise(ERR_CONFIG);
                            end else begin
                                state <= STATE_DISCARD;
                            end
                        end else begin
                            stage_valid <= 1'b1;
                            for (int s = 0; s < SLOTS; s++) begin
                                logic [127:0] row;
                                logic [31:0] key;
                                row = input_data[s*128 +: 128];
                                key = row[31:0];
                                stage_row[s] <= row;
                                stage_pad[s] <= &row;
                                stage_group[s] <= GROUP_BITS'(key >> eff_shift);
                            end
                            if (input_last) begin
                                state <= STATE_DRAIN;
                            end
                        end
                    end
                end
                STATE_DRAIN: begin
                    // the last beat's write and occupancy land this edge
                    state <= STATE_SETTLE;
                end
                STATE_SETTLE: begin
                    if (has_group) begin
                        emit_group <= first_group;
                        state <= STATE_PAIR;
                    end else begin
                        close_out();
                    end
                end
                STATE_PAIR: begin
                    for (int p = 0; p < (SLOTS + 1) / 2; p++) begin
                        for (int w = 0; w < WORDS; w++) begin
                            pair_sum[p][w] <= table_mem[2*p][w][emit_group] + ((2*p + 1 < SLOTS) ? table_mem[2*p+1][w][emit_group] : 64'd0);
                        end
                    end
                    emit_final <= !has_following;
                    state <= STATE_TOTAL;
                end
                STATE_TOTAL: begin
                    for (int w = 0; w < WORDS; w++) begin
                        logic [63:0] acc;
                        acc = 64'd0;
                        for (int p = 0; p < (SLOTS + 1) / 2; p++) begin
                            acc = acc + pair_sum[p][w];
                        end
                        total[w] <= acc;
                    end
                    emit_word <= 3'd0;
                    state <= STATE_EMIT;
                end
                STATE_EMIT: begin
                    if (!output_valid || output_ready) begin
                        if (emit_word == 3'd0) begin
                            output_valid <= 1'b1;
                            output_data <= 64'(emit_group);
                            output_last <= 1'b0;
                            emit_word <= 3'd1;
                        end else if (emit_word <= 3'd4) begin
                            if (active_mask[2'(emit_word - 3'd1)]) begin
                                output_valid <= 1'b1;
                                output_data <= total[emit_word - 3'd1];
                                output_last <= 1'b0;
                            end
                            emit_word <= emit_word + 3'd1;
                        end else begin
                            output_valid <= 1'b1;
                            output_data <= total[4];
                            output_last <= emit_final;
                            aggregated_count <= aggregated_count + total[4];
                            if (emit_final) begin
                                state <= STATE_STATUS;
                                status_valid <= 1'b1;
                                status_error <= 1'b0;
                                status_error_code <= 8'd0;
                                occupied <= '0;
                                batch_open <= 1'b0;
                            end else begin
                                emit_group <= following_group;
                                state <= STATE_PAIR;
                            end
                        end
                    end
                end
                STATE_DISCARD: begin
                    if (input_fire && input_last) begin
                        raise(ERR_CONFIG);
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
                default: state <= STATE_ACCUM;
            endcase
            // the staged beat marks its groups occupied as it accumulates
            if (stage_valid) begin
                for (int b = 0; b < SLOTS; b++) begin
                    if (!stage_pad[b]) begin
                        occupied[stage_group[b]] <= 1'b1;
                    end
                end
            end
        end
    end

    task automatic raise(input logic [7:0] code);
        begin
            batch_open <= 1'b0;
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic close_out;
        begin
            batch_open <= 1'b0;
            occupied <= '0;
            status_valid <= 1'b1;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
            state <= STATE_STATUS;
        end
    endtask
endmodule

`default_nettype wire

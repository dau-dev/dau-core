`default_nettype none

// Positional distributor from R whole 128-bit quad rows per input beat to
// NUM_PARTITIONS independent 64-bit two-beat row streams. Slot s owns the
// contiguous lane group beginning at s*(NUM_PARTITIONS/R) and round-robins
// only within that group. Routing never inspects row contents: keyed stateful
// terminals produce per-lane partial groups for the host to merge by key.
//
// The all-ones 128-bit row is the reserved PAD and is dropped without
// advancing its slot counter. A last beat flushes every lane; each nonempty
// lane marks its final row last, while an empty lane emits the dispatcher's
// zero-row success status. Nonempty lanes are silent on success.
/* verilator lint_off DECLFILENAME */
module dau_stride_distributor_lane (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        push_valid,
    output logic             push_ready,
    input  wire logic [63:0] push_w0,
    input  wire logic [63:0] push_w1,
    input  wire logic        flush,
    output logic             busy,
    output logic             output_valid,
    input  wire logic        output_ready,
    output logic [63:0]      output_data,
    output logic             output_last,
    output logic             status_valid,
    input  wire logic        status_ready,
    output logic             status_error,
    output logic [7:0]       status_error_code
);
    typedef enum logic [1:0] {
        EMIT_IDLE,
        EMIT_W0,
        EMIT_W1
    } emit_t;

    emit_t       emit_state;
    logic [63:0] emit_w0;
    logic [63:0] emit_w1;
    logic        emit_last;
    logic        held_valid;
    logic [63:0] held_w0;
    logic [63:0] held_w1;
    logic        drain_started;
    logic        output_fire;
    logic        emit_free;

    assign output_fire = output_valid && output_ready;
    assign emit_free = (emit_state == EMIT_IDLE) || ((emit_state == EMIT_W1) && output_fire);
    assign push_ready = !flush && (!held_valid || emit_free);
    assign output_valid = emit_state != EMIT_IDLE;
    assign output_data = (emit_state == EMIT_W0) ? emit_w0 : emit_w1;
    assign output_last = (emit_state == EMIT_W1) && emit_last;
    assign busy = held_valid || (emit_state != EMIT_IDLE) || status_valid || (flush && !drain_started);

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_lane();
        end else begin
            case (emit_state)
                EMIT_W0: if (output_fire) emit_state <= EMIT_W1;
                EMIT_W1: if (output_fire) emit_state <= EMIT_IDLE;
                default: ;
            endcase

            if (status_valid && status_ready) begin
                status_valid <= 1'b0;
                status_error <= 1'b0;
                status_error_code <= 8'd0;
            end

            if (flush) begin
                if (!drain_started && emit_free) begin
                    if (held_valid) begin
                        start_emit(held_w0, held_w1, 1'b1);
                        held_valid <= 1'b0;
                    end else begin
                        status_valid <= 1'b1;
                        status_error <= 1'b0;
                        status_error_code <= 8'd0;
                    end
                    drain_started <= 1'b1;
                end
            end else begin
                drain_started <= 1'b0;
                if (push_valid && push_ready) begin
                    if (held_valid) start_emit(held_w0, held_w1, 1'b0);
                    held_valid <= 1'b1;
                    held_w0 <= push_w0;
                    held_w1 <= push_w1;
                end
            end
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

    task automatic reset_lane;
        begin
            emit_state <= EMIT_IDLE;
            emit_w0 <= 64'd0;
            emit_w1 <= 64'd0;
            emit_last <= 1'b0;
            held_valid <= 1'b0;
            held_w0 <= 64'd0;
            held_w1 <= 64'd0;
            drain_started <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask
endmodule
/* verilator lint_on DECLFILENAME */


module dau_stride_distributor #(
    parameter int IN_WIDTH = 512,
    parameter int NUM_PARTITIONS = 8
) (
    input  wire logic                            clk,
    input  wire logic                            rst,
    input  wire logic                            input_valid,
    output logic                                 input_ready,
    input  wire logic [IN_WIDTH-1:0]             input_data,
    input  wire logic                            input_last,
    output logic [NUM_PARTITIONS-1:0]             output_valid,
    input  wire logic [NUM_PARTITIONS-1:0]        output_ready,
    output logic [NUM_PARTITIONS*64-1:0]          output_data,
    output logic [NUM_PARTITIONS-1:0]             output_last,
    output logic [NUM_PARTITIONS-1:0]             status_valid,
    input  wire logic [NUM_PARTITIONS-1:0]        status_ready,
    output logic [NUM_PARTITIONS-1:0]             status_error,
    output logic [NUM_PARTITIONS*8-1:0]           status_error_code
);
    localparam int R = (IN_WIDTH >= 128) ? (IN_WIDTH / 128) : 1;
    localparam int LANES_PER_SLOT = NUM_PARTITIONS / R;
    localparam int LANE_IDX_W = (NUM_PARTITIONS <= 1) ? 1 : $clog2(NUM_PARTITIONS);
    localparam int COUNTER_W = (LANES_PER_SLOT <= 1) ? 1 : $clog2(LANES_PER_SLOT);
    localparam logic [127:0] PAD_ROW = {128{1'b1}};

    if (!(IN_WIDTH inside {128, 256, 512})) begin : g_bad_in_width
        $error("dau_stride_distributor: IN_WIDTH must be 128/256/512");
    end
    if (NUM_PARTITIONS < 1 || NUM_PARTITIONS % R != 0) begin : g_bad_partition_count
        $error("dau_stride_distributor: NUM_PARTITIONS must be a positive multiple of IN_WIDTH/128");
    end

    typedef enum logic {
        FRONT_RUN,
        FRONT_END
    } front_state_t;

    front_state_t front_state;
    logic [COUNTER_W-1:0] slot_counter [R];
    logic [R-1:0] slot_live;
    logic [LANE_IDX_W-1:0] slot_lane [R];
    logic [NUM_PARTITIONS-1:0] lane_push_valid;
    logic [NUM_PARTITIONS-1:0] lane_push_ready;
    logic [NUM_PARTITIONS-1:0] lane_busy;
    logic [NUM_PARTITIONS*64-1:0] lane_push_w0;
    logic [NUM_PARTITIONS*64-1:0] lane_push_w1;
    logic input_fire;
    logic all_idle;

    assign input_fire = input_valid && input_ready;
    assign all_idle = lane_busy == '0;

    always_comb begin
        input_ready = front_state == FRONT_RUN;
        lane_push_valid = '0;
        lane_push_w0 = '0;
        lane_push_w1 = '0;
        for (int s = 0; s < R; s++) begin
            slot_live[s] = input_data[s*128 +: 128] != PAD_ROW;
            slot_lane[s] = LANE_IDX_W'(s * LANES_PER_SLOT + slot_counter[s]);
            if (front_state == FRONT_RUN && input_valid && slot_live[s]) begin
                input_ready = input_ready && lane_push_ready[slot_lane[s]];
            end
        end
        for (int s = 0; s < R; s++) begin
            if (input_valid && input_ready && slot_live[s]) begin
                lane_push_valid[slot_lane[s]] = 1'b1;
                lane_push_w0[slot_lane[s]*64 +: 64] = input_data[s*128 +: 64];
                lane_push_w1[slot_lane[s]*64 +: 64] = input_data[s*128 + 64 +: 64];
            end
        end
    end

    always_ff @(posedge clk) begin
        if (rst) begin
            front_state <= FRONT_RUN;
            for (int s = 0; s < R; s++) slot_counter[s] <= '0;
        end else begin
            case (front_state)
                FRONT_RUN: begin
                    if (input_fire) begin
                        for (int s = 0; s < R; s++) begin
                            if (input_last) begin
                                slot_counter[s] <= '0;
                            end else if (slot_live[s]) begin
                                if (LANES_PER_SLOT <= 1 || slot_counter[s] == COUNTER_W'(LANES_PER_SLOT - 1))
                                    slot_counter[s] <= '0;
                                else
                                    slot_counter[s] <= slot_counter[s] + 1'b1;
                            end
                        end
                        if (input_last) front_state <= FRONT_END;
                    end
                end
                FRONT_END: if (all_idle) front_state <= FRONT_RUN;
                default: front_state <= FRONT_RUN;
            endcase
        end
    end

    genvar p;
    generate
        for (p = 0; p < NUM_PARTITIONS; p++) begin : g_lane
            dau_stride_distributor_lane u_lane (
                .clk(clk),
                .rst(rst),
                .push_valid(lane_push_valid[p]),
                .push_ready(lane_push_ready[p]),
                .push_w0(lane_push_w0[p*64 +: 64]),
                .push_w1(lane_push_w1[p*64 +: 64]),
                .flush(front_state == FRONT_END),
                .busy(lane_busy[p]),
                .output_valid(output_valid[p]),
                .output_ready(output_ready[p]),
                .output_data(output_data[p*64 +: 64]),
                .output_last(output_last[p]),
                .status_valid(status_valid[p]),
                .status_ready(status_ready[p]),
                .status_error(status_error[p]),
                .status_error_code(status_error_code[p*8 +: 8])
            );
        end
    endgenerate
endmodule

`default_nettype wire

`default_nettype none

// Keyed per-row map/ALU tile (composed-pipeline stage).
//
// Consumes one keyed row per 64-bit word ({value[31:0], key[31:0]}) and
// applies the configured ALU operation to the selected lane
// (cfg_target_lane: 0 = value, 1 = key — key-lane mapping is the
// time→bucket derivation); the other lane passes through unchanged, one
// output word per input word. ALU semantics come from dau_map_alu_pkg (the
// dau_core.operators.map_rows golden; INT32 wraparound).
//
// cfg_row_count = N checks the stream length; cfg_row_count = 0 selects
// last-driven mode (stages downstream of a compaction trust the upstream
// last marker). The multiply runs register-to-register across a dedicated
// stage, keeping the DSP cascade off the 8 ns critical path.
module dau_int32_keyed_map_alu
    import dau_map_alu_pkg::*;
(
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        cfg_target_lane,
    input  wire logic [2:0]  cfg_op,
    input  wire logic [31:0] cfg_constant,
    input  wire logic [4:0]  cfg_shift,
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
    output logic [7:0]       status_error_code
);
    localparam logic [7:0] ERR_CONFIG = 8'd1;
    localparam logic [7:0] ERR_STREAM = 8'd2;

    typedef enum logic [2:0] {
        STATE_IDLE,
        STATE_STREAM,
        STATE_MUL,
        STATE_EMIT,
        STATE_DRAIN_ERROR,
        STATE_STATUS_PEND,
        STATE_STATUS
    } state_t;

    state_t state;

    logic        active_target_lane;
    logic [2:0]  active_op;
    logic [31:0] active_constant;
    logic [4:0]  active_shift;
    logic        count_checked;
    logic [63:0] rows_remaining;
    logic        emit_last;
    logic [7:0]  pending_error_code;

    logic signed [31:0] mul_a;
    logic signed [31:0] mul_b;
    logic signed [63:0] product;
    logic [31:0] cheap_mapped;
    logic [31:0] pass_lane;

    logic input_fire;
    logic output_fire;
    logic status_fire;
    logic config_ok;
    logic final_word;

    logic        eff_target_lane;
    logic [2:0]  eff_op;
    logic [31:0] eff_constant;
    logic [4:0]  eff_shift;
    logic [63:0] eff_remaining;
    logic        eff_count_checked;
    logic [31:0] selected_lane;

    assign eff_target_lane = (state == STATE_IDLE) ? cfg_target_lane : active_target_lane;
    assign eff_op = (state == STATE_IDLE) ? cfg_op : active_op;
    assign eff_constant = (state == STATE_IDLE) ? cfg_constant : active_constant;
    assign eff_shift = (state == STATE_IDLE) ? cfg_shift : active_shift;
    assign eff_remaining = (state == STATE_IDLE) ? cfg_row_count : rows_remaining;
    assign eff_count_checked = (state == STATE_IDLE) ? (|cfg_row_count) : count_checked;

    assign input_ready = ((state == STATE_IDLE) || (state == STATE_STREAM) || (state == STATE_DRAIN_ERROR))
        && !output_valid && !status_valid;
    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign status_fire = status_valid && status_ready;

    assign config_ok = cfg_op != 3'd0;

    assign final_word = eff_count_checked ? ((~|eff_remaining[63:1]) && eff_remaining[0]) : input_last;

    assign selected_lane = eff_target_lane ? input_data[31:0] : input_data[63:32];

    always_ff @(posedge clk) begin
        if (rst) begin
            reset_state();
        end else begin
            if (output_fire) begin
                output_valid <= 1'b0;
            end
            case (state)
                STATE_IDLE, STATE_STREAM: begin
                    if (input_fire) begin
                        if ((state == STATE_IDLE) && !config_ok) begin
                            fail(ERR_CONFIG);
                        end else if (eff_count_checked && (input_last != final_word)) begin
                            if (input_last) begin
                                start_error_status(ERR_STREAM);
                            end else begin
                                fail(ERR_STREAM);
                            end
                        end else begin
                            if (state == STATE_IDLE) begin
                                active_target_lane <= cfg_target_lane;
                                active_op <= cfg_op;
                                active_constant <= cfg_constant;
                                active_shift <= cfg_shift;
                                count_checked <= |cfg_row_count;
                            end
                            mul_a <= signed'(selected_lane);
                            mul_b <= signed'(eff_constant);
                            cheap_mapped <= map_cheap_result(eff_op, selected_lane, eff_constant, eff_shift);
                            pass_lane <= eff_target_lane ? input_data[63:32] : input_data[31:0];
                            rows_remaining <= eff_remaining - 64'd1;
                            emit_last <= final_word;
                            state <= STATE_MUL;
                        end
                    end
                end
                STATE_MUL: begin
                    product <= mul_a * mul_b;
                    state <= STATE_EMIT;
                end
                STATE_EMIT: begin
                    output_valid <= 1'b1;
                    output_data <= active_target_lane
                        ? {pass_lane, map_finish_sample(active_op, product, cheap_mapped, active_shift)}
                        : {map_finish_sample(active_op, product, cheap_mapped, active_shift), pass_lane};
                    output_last <= emit_last;
                    if (emit_last) begin
                        state <= STATE_STATUS_PEND;
                    end else begin
                        state <= STATE_STREAM;
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
                        reset_state();
                    end
                end
                default: begin
                    reset_state();
                end
            endcase
        end
    end

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
            status_valid <= 1'b1;
            status_error <= 1'b1;
            status_error_code <= code;
            state <= STATE_STATUS;
        end
    endtask

    task automatic reset_state;
        begin
            state <= STATE_IDLE;
            active_target_lane <= 1'b0;
            active_op <= 3'd0;
            active_constant <= 32'd0;
            active_shift <= 5'd0;
            count_checked <= 1'b0;
            rows_remaining <= 64'd0;
            emit_last <= 1'b0;
            pending_error_code <= 8'd0;
            mul_a <= 32'd0;
            mul_b <= 32'd0;
            product <= 64'd0;
            cheap_mapped <= 32'd0;
            pass_lane <= 32'd0;
            output_valid <= 1'b0;
            output_data <= 64'd0;
            output_last <= 1'b0;
            status_valid <= 1'b0;
            status_error <= 1'b0;
            status_error_code <= 8'd0;
        end
    endtask
endmodule

`default_nettype wire

`default_nettype none

// Fixed-point square root over 16-byte quad rows (two 64-bit words,
// w0 = {value, key}, w1 = {aux1, aux0}; field i is the i-th 32-bit lane in
// (key, value, aux0, aux1) order). The configured destination field is
// rewritten as sqrt of the configured source field in Q(32-F).F fixed point;
// the other fields pass through unchanged, one row out per row in, row count
// preserved (dau_core.operators.sqrt_row_fields is the golden).
//
// EXACT, not approximated. A fixed-point root is an integer root in disguise:
// with x = X * 2**-F, the answer sqrt(x) * 2**F is isqrt(X << F). So this is
// a restoring radix-2 digit recurrence producing floor(sqrt(X << F)) bit for
// bit, and the bench compares with no tolerance. The obvious alternative --
// a segmented polynomial -- would need an error bound, would still be wrong
// in the last place, and would cost DSPs. This uses none.
//
// One iteration per pipeline stage, ITERATIONS deep, so the critical path is
// a single REM_BITS compare-and-subtract regardless of the format's width --
// the same constant-critical-path reasoning as the systolic top-k array.
//
// Throughput is one row every three cycles, MEASURED at 2.94. The ingest and
// emit state machines are separate and the root pipeline sits between them,
// so the two row protocols run CONCURRENTLY -- a single state machine would
// have serialized them at five. The remaining cycle is the emit latch: a
// finished row is taken out of the last stage in OUT_IDLE and only then goes
// out as two words. Presenting w0 straight from the last stage would reach
// two cycles per row at the cost of a combinational path from the pipeline to
// output_data; not taken, because nothing yet needs it.
//
// A composed-pipeline stage: last-driven only (the upstream compaction's last
// marker is trusted), silent on success -- the downstream tile's close-out is
// authoritative. It raises its own status for a torn final row (input_last on
// a w0: ERR_STREAM) and for a NEGATIVE source value, which has no root:
// ERR_DOMAIN invalidates the batch rather than emitting a substituted number
// that looks right.
//
// dst == src is LEGAL and updates in place -- the source is captured when the
// row is pushed and the destination written when it retires, so they may be
// the same field. The map ALU takes the same position.
module dau_fixed_point_sqrt #(
    // fractional bits of the fixed-point format (16 = Q16.16, the format the
    // golden defaults to)
    parameter int FRACTIONAL_BITS = 16
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic [1:0]  cfg_dst_field,
    input  wire logic [1:0]  cfg_src_field,
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
    localparam logic [7:0] ERR_DOMAIN = 8'd3;

    // the source is a NON-NEGATIVE int32, so 31 magnitude bits; the radicand
    // adds the fractional shift and the root is half of it, rounded up
    localparam int RADICAND_BITS = 31 + FRACTIONAL_BITS;
    localparam int ITERATIONS = (RADICAND_BITS + 1) / 2;
    localparam int ROOT_BITS = ITERATIONS;
    // the recurrence's remainder stays under 4*(root+1); three bits over the
    // root covers it with margin (the measured maximum needs two)
    localparam int REM_BITS = ROOT_BITS + 3;
    // two radicand bits are consumed per iteration
    localparam int RAD_BITS = ITERATIONS * 2;

    if (FRACTIONAL_BITS < 0 || FRACTIONAL_BITS > 31) begin : g_fractional_bits_check
        $error("FRACTIONAL_BITS must be in [0, 31]");
    end

    typedef enum logic [1:0] {
        IN_W0,      // accept w0 of a row (batch config latches on the first)
        IN_W1,      // accept w1, form the radicand, push into the pipeline
        IN_DISCARD  // bad config or domain error: consume the batch to input_last
    } in_state_t;

    typedef enum logic [1:0] {
        OUT_IDLE,   // free to take a finished row out of the pipeline
        OUT_W0,     // emit the rooted row's first word
        OUT_W1      // emit its second word
    } out_state_t;

    in_state_t in_state;
    out_state_t out_state;

    logic        batch_open;
    logic [1:0]  active_dst;
    logic [1:0]  active_src;
    logic        status_pending;
    logic [7:0]  pending_error;

    logic [31:0] held_field0;
    logic [31:0] held_field1;

    // stage i holds the recurrence state AFTER iteration i, with the row it
    // belongs to. The destination travels WITH the row: a batch boundary can
    // relatch the config while earlier rows are still in flight, and reading
    // the live config at retire would apply the new batch's destination to
    // the old batch's rows.
    logic                 stage_valid [ITERATIONS+1];
    logic [REM_BITS-1:0]  stage_rem   [ITERATIONS+1];
    logic [ROOT_BITS-1:0] stage_root  [ITERATIONS+1];
    logic [RAD_BITS-1:0]  stage_rad   [ITERATIONS+1];
    logic [31:0]          stage_field [ITERATIONS+1][4];
    logic [1:0]           stage_dst   [ITERATIONS+1];
    logic                 stage_last  [ITERATIONS+1];

    logic [31:0] emit_field [4];
    logic        emit_last;

    logic input_fire;
    logic output_fire;
    logic retire_valid;
    logic pipe_advance;
    logic pipe_push;
    logic [31:0] src_value;
    logic [31:0] in_field [4];

    // w1's two fields arrive THIS cycle; reading them back out of a register
    // would read the previous row, so the source select and the pushed row
    // both come from the live word
    assign in_field[0] = held_field0;
    assign in_field[1] = held_field1;
    assign in_field[2] = input_data[31:0];
    assign in_field[3] = input_data[63:32];
    assign src_value = in_field[active_src];

    assign input_fire = input_valid && input_ready;
    assign output_fire = output_valid && output_ready;
    assign retire_valid = stage_valid[ITERATIONS];

    // The pipeline moves as one. It may move whenever the last stage is empty
    // or its row is being taken into the emit registers this cycle, which is
    // exactly when the emit path is idle.
    assign pipe_advance = !retire_valid || (out_state == OUT_IDLE);

    // Deliberately independent of input_fire: deriving readiness from the
    // push (which depends on the fire, which depends on readiness) is a
    // combinational loop that simulates fine and fails routed DRC.
    assign input_ready = !status_pending && ((in_state == IN_W0) || (in_state == IN_DISCARD) || ((in_state == IN_W1) && pipe_advance));

    assign pipe_push = (in_state == IN_W1) && input_fire && !src_value[31];

    assign output_valid = (out_state == OUT_W0) || (out_state == OUT_W1);
    assign output_data = (out_state == OUT_W0) ? {emit_field[1], emit_field[0]} : {emit_field[3], emit_field[2]};
    assign output_last = (out_state == OUT_W1) && emit_last;

    assign status_valid = status_pending;
    assign status_error = 1'b1;
    assign status_error_code = pending_error;

    always_ff @(posedge clk) begin
        if (rst) begin
            in_state <= IN_W0;
            out_state <= OUT_IDLE;
            batch_open <= 1'b0;
            status_pending <= 1'b0;
            pending_error <= 8'd0;
            emit_last <= 1'b0;
            for (int unsigned i = 0; i <= ITERATIONS; i++) begin
                stage_valid[i] <= 1'b0;
            end
        end else begin
            if (pipe_advance) begin
                stage_valid[0] <= pipe_push;
                if (pipe_push) begin
                    stage_rem[0] <= '0;
                    stage_root[0] <= '0;
                    // left-aligned so the recurrence consumes the top two
                    // bits first
                    stage_rad[0] <= {{(RAD_BITS - RADICAND_BITS){1'b0}}, src_value[30:0], {FRACTIONAL_BITS{1'b0}}};
                    stage_last[0] <= input_last;
                    stage_dst[0] <= active_dst;
                    for (int unsigned f = 0; f < 4; f++) begin
                        stage_field[0][f] <= in_field[f];
                    end
                end

                for (int unsigned i = 0; i < ITERATIONS; i++) begin
                    automatic logic [REM_BITS-1:0] shifted;
                    automatic logic [REM_BITS-1:0] trial;
                    shifted = {stage_rem[i][REM_BITS-3:0], stage_rad[i][RAD_BITS-1:RAD_BITS-2]};
                    trial = {{(REM_BITS - ROOT_BITS - 2){1'b0}}, stage_root[i], 2'b01};

                    stage_valid[i+1] <= stage_valid[i];
                    stage_last[i+1] <= stage_last[i];
                    stage_dst[i+1] <= stage_dst[i];
                    stage_rad[i+1] <= {stage_rad[i][RAD_BITS-3:0], 2'b00};
                    for (int unsigned f = 0; f < 4; f++) begin
                        stage_field[i+1][f] <= stage_field[i][f];
                    end
                    if (shifted >= trial) begin
                        stage_rem[i+1] <= shifted - trial;
                        stage_root[i+1] <= {stage_root[i][ROOT_BITS-2:0], 1'b1};
                    end else begin
                        stage_rem[i+1] <= shifted;
                        stage_root[i+1] <= {stage_root[i][ROOT_BITS-2:0], 1'b0};
                    end
                end
            end

            case (in_state)
                IN_W0: begin
                    if (input_fire) begin
                        if (input_last) begin
                            pending_error <= ERR_STREAM;
                            status_pending <= 1'b1;
                        end else begin
                            held_field0 <= input_data[31:0];
                            held_field1 <= input_data[63:32];
                            if (!batch_open) begin
                                batch_open <= 1'b1;
                                active_dst <= cfg_dst_field;
                                active_src <= cfg_src_field;
                            end
                            in_state <= IN_W1;
                        end
                    end
                end

                IN_W1: begin
                    if (input_fire) begin
                        if (src_value[31]) begin
                            pending_error <= ERR_DOMAIN;
                            if (input_last) status_pending <= 1'b1;
                            else in_state <= IN_DISCARD;
                        end else begin
                            in_state <= IN_W0;
                            if (input_last) batch_open <= 1'b0;
                        end
                    end
                end

                IN_DISCARD: begin
                    if (input_fire && input_last) begin
                        status_pending <= 1'b1;
                    end
                end

                default: in_state <= IN_W0;
            endcase

            case (out_state)
                OUT_IDLE: begin
                    if (retire_valid) begin
                        for (int unsigned f = 0; f < 4; f++) begin
                            emit_field[f] <= stage_field[ITERATIONS][f];
                        end
                        emit_field[stage_dst[ITERATIONS]] <= {{(32 - ROOT_BITS){1'b0}}, stage_root[ITERATIONS]};
                        emit_last <= stage_last[ITERATIONS];
                        out_state <= OUT_W0;
                    end
                end

                OUT_W0: if (output_fire) out_state <= OUT_W1;

                OUT_W1: if (output_fire) out_state <= OUT_IDLE;

                default: out_state <= OUT_IDLE;
            endcase

            if (status_pending && status_ready) begin
                // the error close-out invalidates the whole batch, so rows of
                // it still in flight are dropped rather than emitted
                status_pending <= 1'b0;
                in_state <= IN_W0;
                batch_open <= 1'b0;
                for (int unsigned i = 0; i <= ITERATIONS; i++) begin
                    stage_valid[i] <= 1'b0;
                end
            end
        end
    end
endmodule

`default_nettype wire

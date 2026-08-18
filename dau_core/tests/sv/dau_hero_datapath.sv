`default_nettype none

// The full fused temporal hero datapath in one on-chip stream — no intermediate
// touches DDR:
//
//   as-of ─> market-feature ─> [feature→bucket] ─> time-bucket-key ─>
//     grouped-field-aggregator ─> [bar→roll] ─> rolling-moments
//
// The two bracketed modules are datapath reshape glue (below): a wide feature
// record projected to the bucket tile's row, and a bar record projected to the
// rolling tile's {close, symbol} row (decoding the composite key). The generic
// tiles (as-of, feature, time-bucket-key, grouped-field-aggregation,
// rolling-moments) are unchanged.
//
// Scope: a non-empty single-job demonstration (one reset per run) — the
// success_close_out mid-stages (as-of / feature / aggregator) have their status
// drained and the terminal rolling-moments status closes the chain out, with
// output_last riding through every stage. The multi-job / empty-stage close-out
// protocol is the composition shell's job (as with the align→derive chain).

// feature record (7 words) -> time-bucket-key row (3 words):
//   in:  o0=time, o1={price, symbol}, o2, o3={_, size}, o4, o5=signed_volume, o6
//   out: w0={price, symbol}, w1=time, w2={signed_volume, size}
// Precondition (the fixed-point-fit contract): signed_volume = trade_sign*size
// fits INT32, so its low word o5[31:0] is exact — the host chooses the size
// scale so |signed_volume| < 2^31 (the same rule the bar/rolling INT64 sums
// obey). A size of INT32_MIN would overflow and is out of contract.
module dau_feature_to_bucket (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        input_valid,
    output logic             input_ready,
    input  wire logic [63:0] input_data,
    input  wire logic        input_last,
    output logic             output_valid,
    input  wire logic        output_ready,
    output logic [63:0]      output_data,
    output logic             output_last
);
    typedef enum logic [3:0] {F0, F1, F2, F3, F4, F5, F6, E0, E1, E2} state_t;
    state_t state;
    logic [63:0] r_time;
    logic [31:0] r_price, r_symbol, r_size, r_sv;
    logic        r_last;
    logic fire;

    assign input_ready = (state <= F6) && !output_valid;
    assign fire = input_valid && input_ready;

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= F0; output_valid <= 1'b0; output_last <= 1'b0;
        end else begin
            if (output_valid && output_ready) output_valid <= 1'b0;
            case (state)
                F0: if (fire) begin r_time <= input_data; state <= F1; end
                F1: if (fire) begin r_symbol <= input_data[31:0]; r_price <= input_data[63:32]; state <= F2; end
                F2: if (fire) state <= F3;
                F3: if (fire) begin r_size <= input_data[31:0]; state <= F4; end
                F4: if (fire) state <= F5;
                F5: if (fire) begin r_sv <= input_data[31:0]; state <= F6; end
                F6: if (fire) begin r_last <= input_last; state <= E0; end
                E0: if (!output_valid || output_ready) begin output_valid <= 1'b1; output_data <= {r_price, r_symbol}; output_last <= 1'b0; state <= E1; end
                E1: if (!output_valid || output_ready) begin output_valid <= 1'b1; output_data <= r_time; output_last <= 1'b0; state <= E2; end
                E2: if (!output_valid || output_ready) begin output_valid <= 1'b1; output_data <= {r_sv, r_size}; output_last <= r_last; state <= F0; end
                default: state <= F0;
            endcase
        end
    end
endmodule

// grouped-field-aggregate record (11 words) -> rolling-moments row (1 word):
//   in:  r0={_, key}, r1={f0_last(close), f0_first(open)}, r2..r10
//   out: {close, symbol}   where symbol = key >>> BUCKET_BITS
module dau_bar_to_roll #(
    parameter int BUCKET_BITS = 16
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
    output logic             output_last
);
    typedef enum logic [3:0] {A0, A1, A2, A3, A4, A5, A6, A7, A8, A9, A10, EMIT} state_t;
    state_t state;
    logic [31:0]        r_key;   // the composite key is a bit pattern, not signed
    logic [31:0]        r_close;
    logic               r_last;
    logic fire;

    assign input_ready = (state <= A10) && !output_valid;
    assign fire = input_valid && input_ready;

    always_ff @(posedge clk) begin
        if (rst) begin
            state <= A0; output_valid <= 1'b0; output_last <= 1'b0;
        end else begin
            if (output_valid && output_ready) output_valid <= 1'b0;
            case (state)
                A0: if (fire) begin r_key <= input_data[31:0]; state <= A1; end
                A1: if (fire) begin r_close <= input_data[63:32]; state <= A2; end
                A2, A3, A4, A5, A6, A7, A8, A9: if (fire) state <= state_t'(state + 1);
                A10: if (fire) begin r_last <= input_last; state <= EMIT; end
                EMIT: if (!output_valid || output_ready) begin
                    output_valid <= 1'b1;
                    // {value=close, symbol = key[31:BUCKET_BITS]}: the partition is
                    // packed in the high bits, zero-extended (a LOGICAL extract —
                    // an arithmetic shift would sign-extend a large partition)
                    output_data <= {r_close, 32'(r_key[31:BUCKET_BITS])};
                    output_last <= r_last;
                    state <= A0;
                end
                default: state <= A0;
            endcase
        end
    end
endmodule

module dau_hero_datapath #(
    parameter int BUCKET_BITS = 16,
    parameter int WINDOW = 3
) (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        cfg_has_tolerance,
    input  wire logic [63:0] cfg_tolerance,
    input  wire logic [5:0]  cfg_bucket_shift,
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
    output logic [63:0]      moment_count
);
    // inter-stage streams
    logic        mkt_v, mkt_r, mkt_l;  logic [63:0] mkt_d;   // as-of -> feature
    logic        fe_v, fe_r, fe_l;     logic [63:0] fe_d;    // feature -> proj1
    logic        bk_v, bk_r, bk_l;     logic [63:0] bk_d;    // proj1 -> bucket
    logic        ky_v, ky_r, ky_l;     logic [63:0] ky_d;    // bucket -> aggregator
    logic        ag_v, ag_r, ag_l;     logic [63:0] ag_d;    // aggregator -> proj2
    logic        rl_v, rl_r, rl_l;     logic [63:0] rl_d;    // proj2 -> rolling

    // drained mid-stage statuses (silent for the single-job happy path)
    logic asof_sv, asof_se; logic [7:0] asof_sc; logic [63:0] asof_mc;
    logic feat_sv, feat_se; logic [7:0] feat_sc; logic [63:0] feat_fc;
    logic buck_sv, buck_se; logic [7:0] buck_sc;
    logic agg_sv, agg_se;   logic [7:0] agg_sc;  logic [63:0] agg_gc;

    dau_int32_asof_backward u_asof (
        .clk(clk), .rst(rst), .cfg_has_tolerance(cfg_has_tolerance), .cfg_tolerance(cfg_tolerance),
        .input_valid(input_valid), .input_ready(input_ready), .input_data(input_data), .input_last(input_last),
        .output_valid(mkt_v), .output_ready(mkt_r), .output_data(mkt_d), .output_last(mkt_l),
        .status_valid(asof_sv), .status_ready(1'b1), .status_error(asof_se), .status_error_code(asof_sc), .match_count(asof_mc)
    );
    dau_int32_market_feature u_feature (
        .clk(clk), .rst(rst),
        .input_valid(mkt_v), .input_ready(mkt_r), .input_data(mkt_d), .input_last(mkt_l),
        .output_valid(fe_v), .output_ready(fe_r), .output_data(fe_d), .output_last(fe_l),
        .status_valid(feat_sv), .status_ready(1'b1), .status_error(feat_se), .status_error_code(feat_sc), .feature_count(feat_fc)
    );
    dau_feature_to_bucket u_proj1 (
        .clk(clk), .rst(rst),
        .input_valid(fe_v), .input_ready(fe_r), .input_data(fe_d), .input_last(fe_l),
        .output_valid(bk_v), .output_ready(bk_r), .output_data(bk_d), .output_last(bk_l)
    );
    dau_time_bucket_key #(.BUCKET_BITS(BUCKET_BITS)) u_bucket (
        .clk(clk), .rst(rst), .cfg_bucket_shift(cfg_bucket_shift),
        .input_valid(bk_v), .input_ready(bk_r), .input_data(bk_d), .input_last(bk_l),
        .output_valid(ky_v), .output_ready(ky_r), .output_data(ky_d), .output_last(ky_l),
        .status_valid(buck_sv), .status_ready(1'b1), .status_error(buck_se), .status_error_code(buck_sc)
    );
    dau_grouped_field_aggregation u_agg (
        .clk(clk), .rst(rst),
        .input_valid(ky_v), .input_ready(ky_r), .input_data(ky_d), .input_last(ky_l),
        .output_valid(ag_v), .output_ready(ag_r), .output_data(ag_d), .output_last(ag_l),
        .status_valid(agg_sv), .status_ready(1'b1), .status_error(agg_se), .status_error_code(agg_sc), .group_count(agg_gc)
    );
    dau_bar_to_roll #(.BUCKET_BITS(BUCKET_BITS)) u_proj2 (
        .clk(clk), .rst(rst),
        .input_valid(ag_v), .input_ready(ag_r), .input_data(ag_d), .input_last(ag_l),
        .output_valid(rl_v), .output_ready(rl_r), .output_data(rl_d), .output_last(rl_l)
    );
    logic roll_sv, roll_se; logic [7:0] roll_sc;

    dau_rolling_moments #(.WINDOW(WINDOW)) u_roll (
        .clk(clk), .rst(rst),
        .input_valid(rl_v), .input_ready(rl_r), .input_data(rl_d), .input_last(rl_l),
        .output_valid(output_valid), .output_ready(output_ready), .output_data(output_data), .output_last(output_last),
        .status_valid(roll_sv), .status_ready(status_ready), .status_error(roll_se),
        .status_error_code(roll_sc), .moment_count(moment_count)
    );

    // any mid-stage error is drained by the stage but LATCHED here so it is
    // visible at the chain status (never silently swallowed). On an error the
    // downstream stages never see input_last and would otherwise wait forever;
    // the latched error surfaces so a consumer stops instead of hanging. (Full
    // per-stage flush/recovery is the composition shell's job — the documented
    // single-job scope.)
    logic       err_latched;
    logic [7:0] err_code;
    always_ff @(posedge clk) begin
        if (rst) begin
            err_latched <= 1'b0;
            err_code <= 8'd0;
        end else if (!err_latched) begin
            if (asof_sv && asof_se) begin err_latched <= 1'b1; err_code <= asof_sc; end
            else if (feat_sv && feat_se) begin err_latched <= 1'b1; err_code <= feat_sc; end
            else if (buck_sv && buck_se) begin err_latched <= 1'b1; err_code <= buck_sc; end
            else if (agg_sv && agg_se) begin err_latched <= 1'b1; err_code <= agg_sc; end
        end
    end

    assign status_valid = err_latched ? 1'b1 : roll_sv;
    assign status_error = err_latched ? 1'b1 : roll_se;
    assign status_error_code = err_latched ? err_code : roll_sc;
endmodule

`default_nettype wire

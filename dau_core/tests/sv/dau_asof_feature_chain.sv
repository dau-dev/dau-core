`default_nettype none

// Fused align -> derive chain: the as-of alignment tile streams its market rows
// directly into the market-feature map tile with no reformatting, because the
// as-of output record and the feature-tile input record are the same 4-word
// market row by construction. The chain proves the first fusion stage
// golden-equal in simulation: the aligned+derived features never touch DDR
// between the two tiles.
//
// The as-of tile is success_close_out (a terminal tile) but sits mid-lane here,
// so a small status-join gives the chain ONE faithful close-out. The as-of tile
// is GATED: its status_ready is driven from the chain's own terminal handshake,
// so it holds its status (and its match_count, and its input_ready low) until the
// chain closes out — one job occupies the join at a time, no early rearm. The
// chain closes when the as-of is done AND either the job is empty/errored (close
// on the as-of alone) or the feature tile is also done (the happy path):
//   * happy path (>=1 matched row) — wait for both tiles, forward the feature
//     tile's status, and release both;
//   * empty path (zero matches, no output_last) — synthesize success from the
//     as-of done (the feature tile is idle);
//   * error path — surface the as-of error.
// Single-job-per-reset harness: the real job-boundary protocol (per-job resets,
// draining a partial-output error) is the composition shell's job, not this
// data-path proof.
module dau_asof_feature_chain (
    input  wire logic        clk,
    input  wire logic        rst,
    input  wire logic        cfg_has_tolerance,
    input  wire logic [63:0] cfg_tolerance,
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
    output logic [63:0]      match_count,
    output logic [63:0]      feature_count
);
    // the as-of market-row stream feeding the feature tile
    logic        mkt_valid;
    logic        mkt_ready;
    logic [63:0] mkt_data;
    logic        mkt_last;
    logic        mkt_fire;

    // the as-of tile's own status close-out (drained here into the join)
    logic        asof_status_valid;
    logic        asof_status_error;
    logic [7:0]  asof_status_error_code;
    logic [63:0] asof_match_count;

    // the feature tile's terminal status
    logic        feat_status_valid;
    logic        feat_status_error;
    logic [7:0]  feat_status_error_code;

    // status-join state
    logic any_output;    // the as-of produced at least one market row this job
    logic feat_ready;
    logic synth_only;    // close on the as-of alone (empty success / as-of error)
    logic happy_done;    // both tiles done on a non-empty, non-error job
    logic chain_accept;
    logic [63:0] match_count_reg;  // the as-of count, held past its self-reset

    assign mkt_fire = mkt_valid && mkt_ready;

    dau_int32_asof_backward u_asof (
        .clk(clk),
        .rst(rst),
        .cfg_has_tolerance(cfg_has_tolerance),
        .cfg_tolerance(cfg_tolerance),
        .input_valid(input_valid),
        .input_ready(input_ready),
        .input_data(input_data),
        .input_last(input_last),
        .output_valid(mkt_valid),
        .output_ready(mkt_ready),
        .output_data(mkt_data),
        .output_last(mkt_last),
        .status_valid(asof_status_valid),
        .status_ready(chain_accept),  // gated: as-of holds until the chain closes out
        .status_error(asof_status_error),
        .status_error_code(asof_status_error_code),
        .match_count(asof_match_count)
    );

    // an as-of error aborts the job: flush the feature tile so its in-flight and
    // already-derived output cannot appear after the chain's error close-out, and
    // its per-job state is clean. Held through the error close-out (the as-of
    // holds its error status), released when the chain is accepted.
    logic feat_rst;
    assign feat_rst = rst || (asof_status_valid && asof_status_error);

    dau_int32_market_feature u_feature (
        .clk(clk),
        .rst(feat_rst),
        .input_valid(mkt_valid),
        .input_ready(mkt_ready),
        .input_data(mkt_data),
        .input_last(mkt_last),
        .output_valid(output_valid),
        .output_ready(output_ready),
        .output_data(output_data),
        .output_last(output_last),
        .status_valid(feat_status_valid),
        .status_ready(feat_ready),
        .status_error(feat_status_error),
        .status_error_code(feat_status_error_code),
        .feature_count(feature_count)
    );

    // close on the as-of alone when the job is empty or errored; otherwise wait
    // for the feature tile too. The as-of holds its status (gated status_ready)
    // so asof_status_valid / asof_match_count stay valid until the chain closes.
    assign synth_only = asof_status_valid && (asof_status_error || !any_output);
    assign happy_done = asof_status_valid && feat_status_valid && any_output && !asof_status_error;
    assign status_valid = synth_only || happy_done;
    assign status_error = synth_only ? asof_status_error : feat_status_error;
    assign status_error_code = synth_only ? asof_status_error_code : feat_status_error_code;
    // the as-of holds its live count while its status is asserted; fall back to
    // the latched value after it releases (so the count is never stale during the
    // terminal handshake, even when status_ready is already high)
    assign match_count = asof_status_valid ? asof_match_count : match_count_reg;
    assign chain_accept = status_valid && status_ready;
    // accept the feature tile's status only on the happy close (it is idle on the
    // empty/error paths); the as-of is released by chain_accept above
    assign feat_ready = happy_done && status_ready;

    always_ff @(posedge clk) begin
        if (rst) begin
            any_output <= 1'b0;
            match_count_reg <= 64'd0;
        end else begin
            if (mkt_fire) any_output <= 1'b1;
            if (chain_accept) any_output <= 1'b0;  // rearm for the next job
            // capture the as-of count while it holds its status; it survives the
            // as-of self-reset so a caller reads it with the chain's close-out
            if (asof_status_valid) match_count_reg <= asof_match_count;
        end
    end
endmodule

`default_nettype wire

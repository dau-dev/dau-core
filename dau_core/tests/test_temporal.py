from __future__ import annotations

import pytest

from dau_core.temporal import (
    WORKFLOW_PLAN,
    MarketStream,
    asof_backward,
    deltas,
    dollar_bars,
    dollar_volume,
    ewma,
    gaps,
    generate_market_stream,
    imbalance_bars,
    market_features,
    matched_rows,
    midpoint,
    moment_mean,
    moment_var,
    rolling_max,
    rolling_min,
    rolling_moments,
    rolling_sum,
    signed_volume,
    spread,
    time_bars,
    trade_sign,
    volume_bars,
    vwap_numerator,
)

# --------------------------------------------------------------------------- #
# As-of alignment                                                             #
# --------------------------------------------------------------------------- #


def test_asof_backward_matches_most_recent_quote_including_equal_time() -> None:
    quotes = [(0, 10, 100, 102), (0, 20, 104, 106), (0, 30, 108, 110)]
    trades = [(0, 5, 101, 1), (0, 10, 101, 1), (0, 25, 105, 1), (0, 35, 109, 1)]
    aligned = asof_backward(trades, quotes)
    assert aligned == [
        None,  # before the first quote
        (0, 10, 101, 1, 100, 102),  # equal time takes that quote (backward)
        (0, 25, 105, 1, 104, 106),  # most recent <= 25 is the t=20 quote
        (0, 35, 109, 1, 108, 110),
    ]


def test_asof_backward_equal_time_ties_take_the_last_quote() -> None:
    quotes = [(0, 20, 1, 2), (0, 20, 3, 4)]  # two quotes at t=20
    trades = [(0, 20, 0, 1)]
    assert asof_backward(trades, quotes) == [(0, 20, 0, 1, 3, 4)]  # last one wins


def test_asof_backward_tolerance_makes_a_stale_quote_a_miss() -> None:
    quotes = [(0, 30, 108, 110)]
    trades = [(0, 33, 0, 1), (0, 35, 0, 1)]
    assert asof_backward(trades, quotes, tolerance=4) == [(0, 33, 0, 1, 108, 110), None]


def test_asof_backward_keeps_symbols_independent() -> None:
    quotes = [(0, 10, 1, 2), (1, 5, 30, 31)]
    trades = [(0, 12, 0, 1), (1, 12, 0, 1)]
    assert asof_backward(trades, quotes) == [(0, 12, 0, 1, 1, 2), (1, 12, 0, 1, 30, 31)]


def test_matched_rows_drops_the_misses() -> None:
    aligned = [(0, 1, 0, 0, 0, 0), None, (0, 2, 0, 0, 0, 0)]
    assert matched_rows(aligned) == [(0, 1, 0, 0, 0, 0), (0, 2, 0, 0, 0, 0)]


# --------------------------------------------------------------------------- #
# Market features                                                             #
# --------------------------------------------------------------------------- #


def test_market_feature_primitives() -> None:
    assert midpoint(100, 104) == 102
    assert midpoint(100, 105) == 102  # floor
    assert spread(100, 104) == 4
    assert trade_sign(103, 100, 104) == 1
    assert trade_sign(101, 100, 104) == -1
    assert trade_sign(102, 100, 104) == 0
    assert signed_volume(103, 5, 100, 104) == 5
    assert signed_volume(101, 5, 100, 104) == -5
    assert dollar_volume(103, 5) == 515
    assert vwap_numerator(103, 5) == 515


def test_market_features_derives_every_field() -> None:
    rows = [(0, 10, 103, 5, 100, 104)]
    (f,) = market_features(rows)
    assert (f.midpoint, f.spread, f.trade_sign, f.signed_volume, f.dollar_volume) == (102, 4, 1, 5, 515)


# --------------------------------------------------------------------------- #
# Bars                                                                        #
# --------------------------------------------------------------------------- #


def test_time_bars_bucket_by_floor_and_close_on_bucket_or_symbol_change() -> None:
    rows = [
        (0, 3, 100, 10, 99, 101),
        (0, 7, 110, 20, 99, 101),  # same bucket 0
        (0, 12, 90, 30, 99, 101),  # bucket 1
        (1, 4, 50, 5, 49, 51),  # symbol change
    ]
    bars = time_bars(rows, bar_width=10)
    assert len(bars) == 3
    b0 = bars[0]
    assert (b0.symbol_id, b0.open, b0.high, b0.low, b0.close, b0.volume, b0.count) == (0, 100, 110, 100, 110, 30, 2)
    assert (b0.open_time, b0.close_time) == (3, 7)
    assert bars[1].symbol_id == 0 and bars[1].count == 1 and bars[1].close == 90
    assert bars[2].symbol_id == 1


def test_time_bars_rejects_nonpositive_width() -> None:
    with pytest.raises(ValueError, match="bar_width must be positive"):
        time_bars([], bar_width=0)


def test_volume_bars_close_on_the_crossing_trade_and_emit_the_partial() -> None:
    # sizes 40, 30, 50, 20 with threshold 100 -> [40+30+50=120 closes], [20 partial]
    rows = [(0, t, 100, s, 99, 101) for t, s in ((1, 40), (2, 30), (3, 50), (4, 20))]
    bars = volume_bars(rows, threshold=100)
    assert [b.volume for b in bars] == [120, 20]
    assert [b.count for b in bars] == [3, 1]
    assert (bars[0].open_time, bars[0].close_time) == (1, 3)


def test_dollar_bars_close_on_cumulative_notional() -> None:
    # price*size: 100*3=300, 100*4=400 -> cum 300, 700 (>=600 closes on 2nd)
    rows = [(0, 1, 100, 3, 99, 101), (0, 2, 100, 4, 99, 101), (0, 3, 100, 1, 99, 101)]
    bars = dollar_bars(rows, threshold=600)
    assert [b.dollar for b in bars] == [700, 100]
    assert [b.count for b in bars] == [2, 1]


def test_imbalance_bars_close_on_absolute_net_signed_flow() -> None:
    # buys at 102(+), sells at 98(-) against mid 100; sizes chosen for |cum|>=5
    rows = [
        (0, 1, 102, 3, 99, 101),  # +3
        (0, 2, 102, 3, 99, 101),  # +3 -> cum +6 >= 5 closes
        (0, 3, 98, 4, 99, 101),  # -4 partial
    ]
    bars = imbalance_bars(rows, threshold=5)
    assert [b.signed_volume for b in bars] == [6, -4]
    assert [b.count for b in bars] == [2, 1]


def test_bar_volume_and_dollar_accumulators_are_exact() -> None:
    rows = [(0, t, p, s, p - 1, p + 1) for t, p, s in ((1, 100, 3), (2, 110, 4), (3, 90, 5))]
    (bar,) = time_bars(rows, bar_width=1000)
    assert bar.volume == 12
    assert bar.dollar == 100 * 3 + 110 * 4 + 90 * 5


# --------------------------------------------------------------------------- #
# Rolling + EWMA                                                              #
# --------------------------------------------------------------------------- #


def test_rolling_sum_min_max_warm_up_to_none_then_full_window() -> None:
    vals = [(0, 1), (0, 2), (0, 3), (0, 4), (0, 5)]
    assert rolling_sum(vals, window=3) == [None, None, 6, 9, 12]
    assert rolling_min(vals, window=3) == [None, None, 1, 2, 3]
    assert rolling_max(vals, window=3) == [None, None, 3, 4, 5]


def test_rolling_resets_per_symbol() -> None:
    vals = [(0, 1), (0, 2), (1, 9), (1, 8)]
    assert rolling_sum(vals, window=2) == [None, 3, None, 17]


def test_rolling_moments_and_derived_mean_variance() -> None:
    vals = [(0, 1), (0, 2), (0, 3)]
    moments = rolling_moments(vals, window=3)
    assert moments[:2] == [None, None]
    m = moments[2]
    assert (m.count, m.total, m.total_sq) == (3, 6, 14)
    assert moment_mean(m) == 2.0
    assert moment_var(m, ddof=1) == 1.0
    assert moment_var(m, ddof=0) == pytest.approx(2 / 3)


def test_moment_var_is_exact_at_fixed_point_magnitudes() -> None:
    # three consecutive values near 1e9: a float sum*sum/count subtraction
    # cancels the variance to 0.0; the integer numerator keeps it exact at 1.0
    base = 10**9
    m = rolling_moments([(0, base), (0, base + 1), (0, base + 2)], window=3)[-1]
    assert m is not None
    assert moment_var(m, ddof=1) == 1.0


def test_moment_var_rejects_underdetermined_window() -> None:
    (m,) = rolling_moments([(0, 5)], window=1)
    with pytest.raises(ValueError, match="count > ddof"):
        moment_var(m, ddof=1)


def test_ewma_non_adjusted_recursion_per_symbol() -> None:
    assert ewma([(0, 10), (0, 20), (0, 30)], alpha=0.5) == [10.0, 15.0, 22.5]
    assert ewma([(0, 10), (1, 100)], alpha=0.5) == [10.0, 100.0]  # per-symbol seed


def test_ewma_rejects_out_of_range_alpha() -> None:
    with pytest.raises(ValueError, match="alpha"):
        ewma([(0, 1)], alpha=0.0)


# --------------------------------------------------------------------------- #
# Delta / gap                                                                 #
# --------------------------------------------------------------------------- #


def test_deltas_first_difference_per_symbol() -> None:
    assert deltas([(0, 10), (0, 13), (0, 9), (1, 5)]) == [None, 3, -4, None]


def test_gaps_flag_time_jumps_over_the_threshold() -> None:
    assert gaps([(0, 0), (0, 5), (0, 20), (1, 0)], max_gap=10) == [False, False, True, False]


# --------------------------------------------------------------------------- #
# Fixtures + workflow plan                                                    #
# --------------------------------------------------------------------------- #


def test_generate_market_stream_is_replay_deterministic() -> None:
    a = generate_market_stream(seed=7)
    b = generate_market_stream(seed=7)
    assert a == b
    assert isinstance(a, MarketStream)
    assert generate_market_stream(seed=8) != a


@pytest.mark.parametrize("seed", [0, 1, 3, 42])
def test_generated_streams_are_sorted_and_guarantee_both_paths(seed: int) -> None:
    stream = generate_market_stream(seed=seed, num_symbols=3)
    assert list(stream.quotes) == sorted(stream.quotes, key=lambda r: (r[0], r[1]))
    assert list(stream.trades) == sorted(stream.trades, key=lambda r: (r[0], r[1]))
    aligned = asof_backward(stream.trades, stream.quotes)
    assert len(aligned) == len(stream.trades)
    assert any(row is not None for row in aligned), "no matches"
    assert any(row is None for row in aligned), "no misses (both paths must be exercised)"


def test_generated_stream_feeds_the_full_fused_pipeline() -> None:
    stream = generate_market_stream(seed=5)
    rows = matched_rows(asof_backward(stream.trades, stream.quotes))
    assert market_features(rows)  # derive stage runs
    bars = time_bars(rows, bar_width=3)
    # every bar's volume equals the summed sizes of its constituent trades
    total_bar_volume = sum(b.volume for b in bars)
    assert total_bar_volume == sum(r[3] for r in rows)


def test_workflow_plan_names_a_golden_for_every_stage() -> None:
    assert [entry["stage"] for entry in WORKFLOW_PLAN] == ["align", "derive", "bar", "roll"]
    assert all(entry["golden"] for entry in WORKFLOW_PLAN)

"""The hero workflow (ROADMAP 2a step 4): the predeclared pipeline over the
goldens, and the avoided-materialization cost model."""

from __future__ import annotations

import pytest

from dau_core.hero import materialization_cost, run_hero_workflow
from dau_core.temporal import (
    asof_backward,
    generate_market_stream,
    matched_rows,
    rolling_moments,
    time_bars,
)

_STREAM = generate_market_stream(seed=7, num_symbols=4, quotes_per_symbol=24, trades_per_symbol=60)


def _run(**kwargs):
    return run_hero_workflow(list(_STREAM.trades), list(_STREAM.quotes), **kwargs)


def test_stages_match_the_individual_goldens() -> None:
    result = _run(bar_width=8, roll_window=3)
    market = matched_rows(asof_backward(list(_STREAM.trades), list(_STREAM.quotes)))
    bars = time_bars(market, bar_width=8)
    moments = [m for m in rolling_moments([(b.symbol_id, b.close) for b in bars], window=3) if m is not None]
    assert result.market_rows == market
    assert result.bars == bars
    assert result.moments == moments
    # the per-row feature branch runs and is returned (not discarded), one per
    # market row
    assert len(result.features) == len(market)


def test_the_pipeline_is_a_cardinality_funnel() -> None:
    result = _run(bar_width=8, roll_window=3)
    # input -> market -> bars -> rolling (the feature map is fused, no edge)
    n_input, n_market, n_bar, n_roll = result.funnel
    assert n_input >= n_market >= n_bar >= n_roll
    assert n_bar < n_market  # bars reduce the market rows (the whole point)
    assert [s.out_rows for s in result.stages] == [n_market, n_bar, n_roll]


def test_fusion_avoids_the_intermediate_materialization() -> None:
    result = _run(bar_width=8, roll_window=3)
    cost = materialization_cost(result)
    # staged DMAs every stage's read+write; fused only the endpoints
    assert cost.fused_bytes == cost.input_bytes + cost.final_bytes
    assert cost.staged_bytes > cost.fused_bytes
    assert cost.avoided_bytes == cost.staged_bytes - cost.fused_bytes > 0
    assert cost.dma_ratio > 1.0
    assert (cost.staged_jobs, cost.fused_jobs) == (3, 1)


def test_dma_bound_time_estimate_favours_fusion() -> None:
    result = _run(bar_width=8, roll_window=3)
    cost = materialization_cost(result)
    # a measured-ish envelope: ~600 MB/s effective DMA, ~15 ms per-job tail
    staged, fused = cost.estimate_seconds(dma_bytes_per_sec=600e6, per_job_seconds=0.015)
    assert fused < staged


def test_avoided_bytes_are_dominated_by_the_market_intermediate() -> None:
    from dau_core.hero import MARKET_BYTES

    result = _run(bar_width=8, roll_window=3)
    cost = materialization_cost(result)
    n_market = result.funnel[1]
    # staged writes the market rows (align) and reads them back (bar); fused
    # never touches DDR for them — that avoided round-trip is the bulk of the
    # saving, independent of how the bars later reduce
    assert cost.avoided_bytes >= 2 * n_market * MARKET_BYTES


def test_empty_after_alignment_still_prices() -> None:
    # no trades -> nothing aligns, the whole funnel below the input is empty
    result = run_hero_workflow([], list(_STREAM.quotes), bar_width=8, roll_window=3)
    assert (result.market_rows, result.bars, result.moments) == ([], [], [])
    assert result.funnel == (len(_STREAM.quotes), 0, 0, 0)
    cost = materialization_cost(result)
    assert cost.fused_bytes == cost.input_bytes + cost.final_bytes
    assert cost.final_bytes == 0


def test_fully_empty_workload_has_no_speedup_not_infinity() -> None:
    result = run_hero_workflow([], [], bar_width=8, roll_window=3)
    cost = materialization_cost(result)
    assert cost.staged_bytes == cost.fused_bytes == 0
    assert cost.dma_ratio == 1.0  # 0/0 is no work, not an infinite win


def test_roll_value_selects_the_windowed_bar_price() -> None:
    close = _run(bar_width=8, roll_window=3)
    high = _run(bar_width=8, roll_window=3, roll_value="high")
    # same funnel shape, different windowed prices
    assert close.funnel == high.funnel
    assert close.moments != high.moments


def test_int64_accumulator_roll_values_are_rejected() -> None:
    # volume/dollar are int64 accumulators — the int32 rolling tile can't window
    # them; the model must refuse rather than claim an executable workflow
    for field in ("volume", "dollar", "signed_volume", "symbol_id", "nope"):
        with pytest.raises(ValueError, match="roll_value"):
            _run(bar_width=8, roll_window=3, roll_value=field)


def test_window_below_two_is_rejected() -> None:
    with pytest.raises(ValueError, match="WINDOW >= 2"):
        _run(bar_width=8, roll_window=1)


def test_estimate_rejects_invalid_physical_parameters() -> None:
    cost = materialization_cost(_run(bar_width=8, roll_window=3))
    with pytest.raises(ValueError, match="dma_bytes_per_sec"):
        cost.estimate_seconds(dma_bytes_per_sec=0)
    with pytest.raises(ValueError, match="per_job_seconds"):
        cost.estimate_seconds(dma_bytes_per_sec=600e6, per_job_seconds=-1)

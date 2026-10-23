"""The temporal-finance hero workflow: the predeclared
end-to-end pipeline composed from the :mod:`dau_core.temporal` goldens, plus the
avoided-materialization cost model that is the paper's core evidence::

    quotes ─┐
            ├─> backward as-of ─> [market-feature map] ─> time bars ─> rolling moments
    trades ─┘   (align)           (derive, fused)         (bar)        (roll)

The argument the model makes precise: the pipeline is a *cardinality funnel*.
The align stage emits one market row per matched trade (``N_mkt``); the bar stage
reduces those to a handful of bars per symbol (``N_bar << N_mkt``); the roll stage
runs over the bars. A **staged** execution writes each stage's output to DDR and
the next stage reads it back — so it pays to materialize the large ``N_mkt`` market
intermediate the bar reduction is about to discard. A **fused** execution keeps
every intermediate on-chip and touches DDR only for the input stream and the
final rolling output. The gain is not a magic primitive: it is the avoided DMA of
the intermediate the bar reduction discards.

The per-row market-feature derivation (midpoint/spread/sign/signed_volume/dollar)
is a map that fuses into the bar tile's front chain stage (the q1/q6 shape) — it
adds no materialized intermediate in either execution — so the cost model's
materializing stages are align → bar → roll. ``market_features`` is still run and
returned as the feature output branch (not discarded).

Both paths are DMA-bound (compute overlapped), so the model computes each from
the exact golden cardinalities and the measured per-tile record widths — a
first-order estimate the silicon run validates, not a measured result.

No dataframe or numerical-runtime dependency: pure integer arithmetic over
the temporal goldens.
"""

from __future__ import annotations

from ccflow import ResultBase
from pydantic import ConfigDict

from .models import CheckedMixin, CheckedModel
from .temporal import (
    MarketFeatures,
    MarketRow,
    QuoteRow,
    RollingMoment,
    TemporalBar,
    TradeRow,
    asof_backward,
    market_features,
    matched_rows,
    rolling_moments,
    time_bars,
)

# On-wire record widths (bytes) — the exact tile row formats (8-byte words). Each
# stage reads its tile's input record and writes its tile's output record; a
# staged run DMAs both, a fused run DMAs only the pipeline endpoints.
INPUT_BYTES = 3 * 8  # as-of merged input: {rec_type, symbol}, time, {fieldB, fieldA}
MARKET_BYTES = 4 * 8  # as-of market row: time, {price, symbol}, {ask, bid}, {_, size}
BAR_BYTES = 6 * 8  # bar record: OHLC + volume/dollar/signed accumulators + count
ROLL_INPUT_BYTES = 1 * 8  # rolling-moment input: one {value, symbol} word (a bar projection)
ROLL_BYTES = 3 * 8  # rolling-moment record: {count, symbol}, total, total_sq

# The bar fields the int32 rolling-moment tile can window: the OHLC prices are in
# the int32 price domain and fit the tile's signed-int32 {value, symbol} word.
# The volume/dollar/signed accumulators are int64 (a bar sums many trades) — they
# would overflow the int32 tile, so rolling them needs the wide rolling variant
# (a follow-up), not this model.
ROLL_FIELDS = ("open", "high", "low", "close")


class WorkflowStage(CheckedModel):
    """One materializing stage and its cardinality: ``in_rows`` records of
    ``in_bytes`` in, ``out_rows`` records of ``out_bytes`` out (the tile's own
    input/output record widths). A staged run DMAs both; a fused run DMAs
    neither (except the pipeline endpoints)."""

    name: str
    op: str
    in_rows: int
    in_bytes: int
    out_rows: int
    out_bytes: int

    @property
    def read_bytes(self) -> int:
        return self.in_rows * self.in_bytes

    @property
    def write_bytes(self) -> int:
        return self.out_rows * self.out_bytes


class HeroResult(CheckedMixin, ResultBase):
    """The executed workflow: the materializing-stage cardinalities (the funnel),
    the per-row feature branch, and the stage outputs (for a bit-exact
    comparison against a CPU reference)."""

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    stages: tuple[WorkflowStage, ...]
    market_rows: tuple[MarketRow, ...]
    features: tuple[MarketFeatures, ...]
    bars: tuple[TemporalBar, ...]
    moments: tuple[RollingMoment, ...]

    @property
    def funnel(self) -> tuple[int, ...]:
        """Row counts down the reduction path: input → market → bars → rolling
        (the fused feature map does not change cardinality)."""
        return (self.stages[0].in_rows, *(stage.out_rows for stage in self.stages))


def run_hero_workflow(
    trades: list[TradeRow],
    quotes: list[QuoteRow],
    *,
    bar_width: int,
    roll_window: int,
    tolerance: int | None = None,
    roll_value: str = "close",
) -> HeroResult:
    """Run the predeclared workflow end to end over the goldens, recording each
    materializing stage's cardinality. ``roll_value`` selects the per-bar OHLC
    price the roll stage windows (:data:`ROLL_FIELDS`); ``roll_window`` is the
    count-based window (``>= 2``, the tile's minimum). Both input streams must be
    pre-sorted by ``(symbol_id, time)`` (the shared as-of / bar precondition)."""
    if roll_value not in ROLL_FIELDS:
        raise ValueError(f"roll_value must be one of {ROLL_FIELDS} (the int32 bar prices the tile can window), got {roll_value!r}")
    if roll_window < 2:
        raise ValueError(f"roll_window must be >= 2 (rolling-moments requires WINDOW >= 2), got {roll_window}")
    trades = list(trades)
    quotes = list(quotes)
    n_input = len(trades) + len(quotes)  # the merged stream the as-of tile reads

    market = matched_rows(asof_backward(trades, quotes, tolerance=tolerance))
    features = market_features(market)  # the fused per-row derivation, kept as an output branch
    bars = time_bars(market, bar_width=bar_width)
    values = [(bar.symbol_id, getattr(bar, roll_value)) for bar in bars]
    moments = [moment for moment in rolling_moments(values, window=roll_window) if moment is not None]

    stages = (
        WorkflowStage(name="align", op="backward as-of", in_rows=n_input, in_bytes=INPUT_BYTES, out_rows=len(market), out_bytes=MARKET_BYTES),
        WorkflowStage(
            name="bar", op="market-feature map + time bars", in_rows=len(market), in_bytes=MARKET_BYTES, out_rows=len(bars), out_bytes=BAR_BYTES
        ),
        WorkflowStage(name="roll", op="rolling moments", in_rows=len(bars), in_bytes=ROLL_INPUT_BYTES, out_rows=len(moments), out_bytes=ROLL_BYTES),
    )
    return HeroResult(stages=stages, market_rows=market, features=features, bars=bars, moments=moments)


class MaterializationCost(CheckedMixin, ResultBase):
    """The avoided-materialization accounting for one workflow run. ``staged``
    DMAs every stage's read and write; ``fused`` DMAs only the pipeline endpoints
    (the input stream in, the final rolling output out). ``avoided`` is the
    intermediate materialization the fusion removes; ``dma_ratio`` is the
    DMA-bound speedup (both paths are DMA-bound with compute overlapped)."""

    model_config = ConfigDict(frozen=True, revalidate_instances="always")

    staged_bytes: int
    fused_bytes: int
    avoided_bytes: int
    input_bytes: int
    final_bytes: int
    staged_jobs: int
    fused_jobs: int

    @property
    def dma_ratio(self) -> float:
        """Staged / fused DMA. A no-work run (both zero) has no speedup (1.0);
        a genuinely nonzero staged cost with a zero fused cost is unbounded."""
        if self.fused_bytes == 0:
            return 1.0 if self.staged_bytes == 0 else float("inf")
        return self.staged_bytes / self.fused_bytes

    def estimate_seconds(self, *, dma_bytes_per_sec: float, per_job_seconds: float = 0.0) -> tuple[float, float]:
        """(staged, fused) wall-clock estimates: DMA time plus a per-job tail
        (device setup + the one-job compute tail the overlap runner cannot hide).
        A DMA-bound first-order model — the prediction the silicon run checks."""
        if dma_bytes_per_sec <= 0:
            raise ValueError(f"dma_bytes_per_sec must be positive, got {dma_bytes_per_sec}")
        if per_job_seconds < 0:
            raise ValueError(f"per_job_seconds must be non-negative, got {per_job_seconds}")
        staged = self.staged_bytes / dma_bytes_per_sec + self.staged_jobs * per_job_seconds
        fused = self.fused_bytes / dma_bytes_per_sec + self.fused_jobs * per_job_seconds
        return staged, fused


def materialization_cost(result: HeroResult) -> MaterializationCost:
    """Fused vs staged DMA accounting for an executed workflow. Staged pays each
    stage's read + write (the intermediate is written by one job and read back by
    the next); fused pays only the input read and the final write."""
    staged = sum(stage.read_bytes + stage.write_bytes for stage in result.stages)
    input_bytes = result.stages[0].read_bytes
    final_bytes = result.stages[-1].write_bytes
    fused = input_bytes + final_bytes
    return MaterializationCost(
        staged_bytes=staged,
        fused_bytes=fused,
        avoided_bytes=staged - fused,
        input_bytes=input_bytes,
        final_bytes=final_bytes,
        staged_jobs=len(result.stages),
        fused_jobs=1,
    )

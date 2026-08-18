"""Golden models for the temporal-finance hero workflow.

The fused workflow this file is the CPU reference for:

    quotes ─┐
            ├─> backward as-of ─> midpoint/spread/sign ─┐
    trades ─┘                                           ├─> time/vol/$ bars
                                                        └─> rolling features

Everything here operates on the **encoded integer domain** the hardware sees —
symbol ids are int32 dictionary codes, timestamps are int64, prices/sizes are
int64 fixed-point (the host quantizes at a chosen scale before staging, exactly
as ``dau_polars.ohlcv`` does with its micro-unit ``SCALE``). The models are
therefore scale-agnostic integer arithmetic: a caller that quantizes price and
size at scales ``Sp`` and ``Ss`` gets dollar volume in ``Sp*Ss`` units and must
express dollar-bar thresholds in the same units. Division is **deferred** (Q100
discipline): bars and rolling windows emit exact integer accumulators
(sum / sum-of-squares / count) and the host divides once, so every value on the
wire is exact.

State is **bounded and per-symbol**: the as-of tile holds one current quote per
symbol partition; a bar tile holds one open bar; a rolling tile holds one
window. The pre-sort precondition (both streams ordered by ``(symbol_id, time)``)
is the same one the sorted-scan tiles already require — the host sorts, or the
range-partitioner + sorter path does.

These are semantic references, ahead of the HDL. The ``int64`` timestamp / sum
tiles and the as-of / windowed-stat tiles byte-compare against them.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TypeVar

_T = TypeVar("_T")

TEMPORAL_CONTRACT_VERSION = "0.1"

# Row conventions in the encoded domain (all fields int; symbol_id int32):
#   quote row  = (symbol_id, time, bid, ask)
#   trade row  = (symbol_id, time, price, size)
#   market row = (symbol_id, time, price, size, bid, ask)   # a trade + its quote
QuoteRow = tuple[int, int, int, int]
TradeRow = tuple[int, int, int, int]
MarketRow = tuple[int, int, int, int, int, int]


# The workflow logical plan, written down (publishable even when the raw market
# data stays private): an ordered list of (stage, semantic op, golden). A
# capture/planner pass matches a user's Polars plan against these
# stage semantics; the tile compositions realize them.
WORKFLOW_PLAN: tuple[dict[str, str], ...] = (
    {"stage": "align", "op": "join_asof(strategy=backward, by=symbol, tolerance=T)", "golden": "asof_backward"},
    {"stage": "derive", "op": "midpoint, spread, trade_sign, signed_volume, dollar_volume, vwap_numerator", "golden": "market_features"},
    {
        "stage": "bar",
        "op": "time | volume | dollar | imbalance bars (OHLCV + $ + signed + vwap-num)",
        "golden": "time_bars / volume_bars / dollar_bars / imbalance_bars",
    },
    {
        "stage": "roll",
        "op": "rolling sum/mean/min/max/var + EWMA over the reduced bar stream",
        "golden": "rolling_sum / rolling_min / rolling_max / rolling_moments / ewma",
    },
)


def asof_backward(
    trades: Iterable[TradeRow],
    quotes: Iterable[QuoteRow],
    *,
    tolerance: int | None = None,
) -> list[MarketRow | None]:
    """Backward as-of join: each trade attached to the most recent quote for
    the same symbol whose ``quote_time <= trade_time`` (and, when ``tolerance``
    is set, ``trade_time - quote_time <= tolerance``). Emits one entry per input
    trade, in input order — a matched ``(symbol_id, time, price, size, bid,
    ask)`` market row, or ``None`` when no quote qualifies (the miss is a typed
    residue; :func:`matched_rows` drops them).

    Matches ``polars.DataFrame.join_asof(strategy="backward", by="symbol",
    tolerance=...)``: on equal timestamps the *last* qualifying quote wins.
    Both streams must be pre-sorted by ``(symbol_id, time)``. The tile holds one
    current quote per symbol partition and advances it monotonically — bounded
    state, one pass.
    """
    per_symbol_quotes: dict[int, list[QuoteRow]] = {}
    for quote in quotes:
        per_symbol_quotes.setdefault(quote[0], []).append(tuple(quote))  # type: ignore[arg-type]

    cursor: dict[int, int] = {}  # symbol -> index of the held (current) quote, -1 = none yet
    out: list[MarketRow | None] = []
    for symbol_id, time, price, size in (tuple(trade) for trade in trades):  # type: ignore[misc]
        qs = per_symbol_quotes.get(symbol_id, ())
        idx = cursor.get(symbol_id, -1)
        # advance while the NEXT quote is still <= this trade's time (lands on
        # the last quote with quote_time <= time; equal-time ties take the last)
        while idx + 1 < len(qs) and qs[idx + 1][1] <= time:
            idx += 1
        cursor[symbol_id] = idx
        if idx < 0:
            out.append(None)
            continue
        _, quote_time, bid, ask = qs[idx]
        if tolerance is not None and time - quote_time > tolerance:
            out.append(None)
        else:
            out.append((symbol_id, time, price, size, bid, ask))
    return out


def matched_rows(aligned: Iterable[MarketRow | None]) -> list[MarketRow]:
    """Drop the as-of misses — the aligned market rows the downstream fused
    stages consume."""
    return [row for row in aligned if row is not None]


def midpoint(bid: int, ask: int) -> int:
    """``(bid + ask) // 2`` (floor). The quote midpoint in the price scale."""
    return (bid + ask) // 2


def spread(bid: int, ask: int) -> int:
    """``ask - bid`` in the price scale."""
    return ask - bid


def trade_sign(price: int, bid: int, ask: int) -> int:
    """The stateless quote-rule trade sign: ``+1`` above the midpoint, ``-1``
    below, ``0`` at it. (The tick-test tie-break needs prior state and is a
    later windowed variant.)"""
    mid = midpoint(bid, ask)
    if price > mid:
        return 1
    if price < mid:
        return -1
    return 0


def signed_volume(price: int, size: int, bid: int, ask: int) -> int:
    """``trade_sign * size`` — size credited to the initiating side."""
    return trade_sign(price, bid, ask) * size


def dollar_volume(price: int, size: int) -> int:
    """``price * size`` — notional in ``price_scale * size_scale`` units (the
    VWAP numerator; exact, host rescales)."""
    return price * size


def vwap_numerator(price: int, size: int) -> int:
    """Alias of :func:`dollar_volume`: VWAP = ``sum(price*size) / sum(size)``,
    accumulated over a bar and divided once by the host."""
    return price * size


@dataclass(frozen=True)
class MarketFeatures:
    """The per-row derived features of one aligned market row."""

    symbol_id: int
    time: int
    price: int
    size: int
    midpoint: int
    spread: int
    trade_sign: int
    signed_volume: int
    dollar_volume: int


def market_features(rows: Iterable[MarketRow]) -> list[MarketFeatures]:
    """Derive the fused per-row market features for a stream of aligned market
    rows (the ``derive`` stage of the workflow plan)."""
    out: list[MarketFeatures] = []
    for symbol_id, time, price, size, bid, ask in rows:
        out.append(
            MarketFeatures(
                symbol_id=symbol_id,
                time=time,
                price=price,
                size=size,
                midpoint=midpoint(bid, ask),
                spread=spread(bid, ask),
                trade_sign=trade_sign(price, bid, ask),
                signed_volume=signed_volume(price, size, bid, ask),
                dollar_volume=dollar_volume(price, size),
            )
        )
    return out


INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1
"""The on-wire accumulator width. The bar/rolling sums are ``int64`` on the
stream contract, so the host's encoding audit must choose
price/size scales such that a bar's ``volume``/``dollar``/``signed_volume``
accumulators stay in ``[INT64_MIN, INT64_MAX]`` — the same fixed-point-fit rule
the TPC-H columns already obey. These goldens compute in exact Python integers
(the true in-range semantics); staging values that would overflow int64 is a
host error, not silently wrapped here. The fixture generator stays well within
range (micro-dollar prices × integer share counts)."""


@dataclass(frozen=True)
class TemporalBar:
    """One closed bar over aligned market rows. OHLC on price, plus exact
    integer accumulators (volume, dollar/vwap-numerator, signed volume, count)
    so VWAP and averages divide once on the host — ``int64`` on the wire (see
    :data:`INT64_MAX`). ``open_time``/``close_time`` carry the ordering keys for
    associative cross-batch merge."""

    symbol_id: int
    open: int
    high: int
    low: int
    close: int
    volume: int
    dollar: int
    signed_volume: int
    count: int
    open_time: int
    close_time: int

    def _extend(self, time: int, price: int, size: int, signed: int) -> TemporalBar:
        return TemporalBar(
            symbol_id=self.symbol_id,
            open=self.open,
            high=max(self.high, price),
            low=min(self.low, price),
            close=price,
            volume=self.volume + size,
            dollar=self.dollar + price * size,
            signed_volume=self.signed_volume + signed,
            count=self.count + 1,
            open_time=self.open_time,
            close_time=time,
        )


def _open_bar(symbol_id: int, time: int, price: int, size: int, signed: int) -> TemporalBar:
    return TemporalBar(
        symbol_id=symbol_id,
        open=price,
        high=price,
        low=price,
        close=price,
        volume=size,
        dollar=price * size,
        signed_volume=signed,
        count=1,
        open_time=time,
        close_time=time,
    )


def time_bars(rows: Iterable[MarketRow], *, bar_width: int) -> list[TemporalBar]:
    """Fixed-width time bars: trades bucket by ``time // bar_width``; a bar spans
    one ``(symbol_id, bucket)`` and closes when the bucket or symbol changes.
    Rows must be pre-sorted by ``(symbol_id, time)``. Matches
    ``group_by_dynamic(every=bar_width, closed="left")`` per symbol."""
    if bar_width <= 0:
        raise ValueError(f"bar_width must be positive, got {bar_width}")
    out: list[TemporalBar] = []
    current: TemporalBar | None = None
    current_bucket: int | None = None
    for symbol_id, time, price, size, bid, ask in rows:
        signed = signed_volume(price, size, bid, ask)
        bucket = time // bar_width
        if current is None or symbol_id != current.symbol_id or bucket != current_bucket:
            if current is not None:
                out.append(current)
            current = _open_bar(symbol_id, time, price, size, signed)
            current_bucket = bucket
        else:
            current = current._extend(time, price, size, signed)
    if current is not None:
        out.append(current)
    return out


def _accumulator_bars(rows: Iterable[MarketRow], *, threshold: int, metric: str) -> list[TemporalBar]:
    """Threshold-clock bars: accumulate ``metric`` per symbol and close the bar
    on the trade that reaches ``threshold`` (the crossing trade is included —
    the López de Prado convention). A symbol change closes the open bar. Emits a
    trailing partial bar at end-of-stream."""
    if threshold <= 0:
        raise ValueError(f"threshold must be positive, got {threshold}")
    out: list[TemporalBar] = []
    current: TemporalBar | None = None
    for symbol_id, time, price, size, bid, ask in rows:
        signed = signed_volume(price, size, bid, ask)
        if current is not None and symbol_id != current.symbol_id:
            out.append(current)
            current = None
        current = _open_bar(symbol_id, time, price, size, signed) if current is None else current._extend(time, price, size, signed)
        if metric == "volume":
            reached = current.volume
        elif metric == "dollar":
            reached = current.dollar
        else:  # imbalance
            reached = abs(current.signed_volume)
        if reached >= threshold:
            out.append(current)
            current = None
    if current is not None:
        out.append(current)
    return out


def volume_bars(rows: Iterable[MarketRow], *, threshold: int) -> list[TemporalBar]:
    """Volume bars: close when cumulative traded size reaches ``threshold``."""
    return _accumulator_bars(rows, threshold=threshold, metric="volume")


def dollar_bars(rows: Iterable[MarketRow], *, threshold: int) -> list[TemporalBar]:
    """Dollar bars: close when cumulative notional (``sum price*size``) reaches
    ``threshold`` (same units as :func:`dollar_volume`)."""
    return _accumulator_bars(rows, threshold=threshold, metric="dollar")


def imbalance_bars(rows: Iterable[MarketRow], *, threshold: int) -> list[TemporalBar]:
    """Imbalance bars: close when ``|cumulative signed volume|`` reaches
    ``threshold`` — a bar per unit of net directional flow."""
    return _accumulator_bars(rows, threshold=threshold, metric="imbalance")


# Rolling windows are count-based (the last ``window`` observations) and reset
# per symbol — matching ``pl.col(x).rolling_*(window_size=w).over("symbol")``
# with the default ``min_periods == window`` (warm-up rows are ``None``).


def _rolling(values: Iterable[tuple[int, int]], *, window: int, reduce: Callable[[list[int]], _T]) -> list[_T | None]:
    if window <= 0:
        raise ValueError(f"window must be positive, got {window}")
    out: list[_T | None] = []
    buffers: dict[int, list[int]] = {}
    for symbol_id, value in values:
        buf = buffers.setdefault(symbol_id, [])
        buf.append(value)
        if len(buf) > window:
            buf.pop(0)
        out.append(reduce(buf) if len(buf) == window else None)
    return out


def rolling_sum(values: Iterable[tuple[int, int]], *, window: int) -> list[int | None]:
    """Per-symbol rolling sum of the last ``window`` values (exact int)."""
    return _rolling(values, window=window, reduce=sum)


def rolling_min(values: Iterable[tuple[int, int]], *, window: int) -> list[int | None]:
    """Per-symbol rolling minimum of the last ``window`` values."""
    return _rolling(values, window=window, reduce=min)


def rolling_max(values: Iterable[tuple[int, int]], *, window: int) -> list[int | None]:
    """Per-symbol rolling maximum of the last ``window`` values."""
    return _rolling(values, window=window, reduce=max)


@dataclass(frozen=True)
class RollingMoment:
    """The exact integer accumulators of one full rolling window — mean and
    variance divide once on the host (:func:`moment_mean`, :func:`moment_var`)."""

    count: int
    total: int
    total_sq: int


def rolling_moments(values: Iterable[tuple[int, int]], *, window: int) -> list[RollingMoment | None]:
    """Per-symbol rolling ``(count, sum, sum_of_squares)`` over the last
    ``window`` values — the deferred-division form of rolling mean/variance.

    REFUSES a window whose sum of squares leaves a signed 64-bit accumulator.
    That is the tile's arithmetic precondition, and it binds inside the int32
    domain: ``(2**31-1)**2`` is already ``2**61``, so a full-range int32 column
    overflows ``sum_sq`` from window 3. The tile cannot check it — it sees one
    row at a time and has no bound to compare against — and it wraps silently
    into a wrong variance rather than erroring. Python integers do not wrap, so
    a golden that computed the exact value would DISAGREE with the hardware
    over exactly the inputs the contract excludes, and the bench comparing the
    two would be the only thing that ever noticed. Raising instead keeps the
    reference and the datapath from ever quietly parting company: inside the
    precondition they are equal, and outside it neither claims an answer.

    The proof properly lives further upstream — dau-polars refuses to route a
    rolling scan whose value column declares no ``magnitude_bits`` — so this is
    a backstop for a direct caller, not the primary check."""

    limit = 1 << 63

    def reduce(buf: list[int]) -> RollingMoment:
        total_sq = sum(v * v for v in buf)
        if not -limit <= total_sq <= limit - 1:
            raise ValueError(
                f"rolling window of {len(buf)} values needs {total_sq.bit_length()} bits of sum_sq, "
                f"more than the tile's INT64; reduce the window or rescale the column"
            )
        return RollingMoment(count=len(buf), total=sum(buf), total_sq=total_sq)

    return _rolling(values, window=window, reduce=reduce)


def moment_mean(moment: RollingMoment) -> float:
    """Mean from a rolling moment."""
    return moment.total / moment.count


def moment_var(moment: RollingMoment, *, ddof: int = 1) -> float:
    """Variance from a rolling moment (``ddof=1`` sample variance, matching
    ``polars.rolling_var``). Raises on an under-determined window.

    The numerator ``count*sum_sq - sum*sum`` is formed in **exact integers**
    before the single float divide — dividing ``sum*sum`` first (float) would
    cancel the variance away at fixed-point magnitudes (e.g. three consecutive
    values near ``1e9``)."""
    denom = moment.count - ddof
    if denom <= 0:
        raise ValueError(f"variance needs count > ddof, got count={moment.count} ddof={ddof}")
    numerator = moment.count * moment.total_sq - moment.total * moment.total
    return numerator / (moment.count * denom)


def moment_std(moment: RollingMoment, *, ddof: int = 1) -> float:
    """Sample standard deviation from a rolling moment.

    The root of :func:`moment_var`, and deliberately defined in terms of it
    rather than independently: polars' ``rolling_std`` is the root of its
    ``rolling_var``, so any other formulation would disagree with the
    reference in the last place for no benefit.

    On device this is where the divide and sqrt tiles land -- variance
    divides before it roots -- which is why the numerator stays exact
    integers right up to the single divide.
    """
    return math.sqrt(moment_var(moment, ddof=ddof))


def ewma(values: Iterable[tuple[int, int]], *, alpha: float) -> list[float]:
    """Per-symbol exponentially weighted moving average, non-adjusted recursion
    (``e[0] = x[0]``; ``e[i] = alpha*x[i] + (1-alpha)*e[i-1]``) — matches
    ``pl.col(x).ewm_mean(alpha=alpha, adjust=False).over("symbol")``. Float: the
    reference real-valued semantics the fixed-point tile approximates under its
    declared error budget."""
    if not 0.0 < alpha <= 1.0:
        raise ValueError(f"alpha must be in (0, 1], got {alpha}")
    out: list[float] = []
    state: dict[int, float] = {}
    for symbol_id, value in values:
        prev = state.get(symbol_id)
        cur = float(value) if prev is None else alpha * value + (1.0 - alpha) * prev
        state[symbol_id] = cur
        out.append(cur)
    return out


def deltas(values: Iterable[tuple[int, int]]) -> list[int | None]:
    """Per-symbol first difference (``value[i] - value[i-1]``; ``None`` for the
    first row of each symbol) — matches ``pl.col(x).diff().over("symbol")``."""
    out: list[int | None] = []
    prev: dict[int, int] = {}
    for symbol_id, value in values:
        p = prev.get(symbol_id)
        out.append(None if p is None else value - p)
        prev[symbol_id] = value
    return out


def gaps(times: Iterable[tuple[int, int]], *, max_gap: int) -> list[bool]:
    """Per-symbol gap flags: ``True`` where ``time[i] - time[i-1] > max_gap``
    (the first row of each symbol is never a gap)."""
    out: list[bool] = []
    prev: dict[int, int] = {}
    for symbol_id, time in times:
        p = prev.get(symbol_id)
        out.append(False if p is None else (time - p) > max_gap)
        prev[symbol_id] = time
    return out


@dataclass(frozen=True)
class MarketStream:
    """A deterministic (seeded) quote/trade fixture in the encoded domain, both
    streams sorted by ``(symbol_id, time)``."""

    quotes: tuple[QuoteRow, ...]
    trades: tuple[TradeRow, ...]


def generate_market_stream(
    *,
    seed: int,
    num_symbols: int = 4,
    quotes_per_symbol: int = 24,
    trades_per_symbol: int = 40,
    price_scale: int = 1_000_000,
) -> MarketStream:
    """Generate a deterministic quote/trade fixture: per symbol, a random-walk
    two-sided quote (micro-dollar prices) and interleaved trades in integer
    share sizes priced near the prevailing midpoint — so ``dollar_volume``
    accumulators stay comfortably in int64. Same ``seed`` ⇒ byte-identical
    output (replay determinism). Uses only the stdlib ``random`` —
    zero-dependency, like the rest of dau-core.

    **Both paths are exercised by construction**: one trade per symbol is placed
    just before that symbol's first quote, so :func:`asof_backward` always
    produces at least one miss as well as matches."""
    import random

    if num_symbols <= 0 or quotes_per_symbol <= 0 or trades_per_symbol <= 0:
        raise ValueError("symbol / quote / trade counts must be positive")
    rng = random.Random(seed)
    quotes: list[QuoteRow] = []
    trades: list[TradeRow] = []
    for symbol_id in range(num_symbols):
        mid = rng.randint(50, 500) * price_scale  # a per-symbol base price
        time = 1  # first quote lands at >= 2, leaving room for a pre-quote miss
        quote_times: list[tuple[int, int, int]] = []  # (time, bid, ask)
        for _ in range(quotes_per_symbol):
            time += rng.randint(1, 5)
            mid += rng.randint(-3, 3) * (price_scale // 100)  # ~cent-scale walk
            half = rng.randint(1, 5) * (price_scale // 100)
            bid, ask = mid - half, mid + half
            quote_times.append((time, bid, ask))
            quotes.append((symbol_id, time, bid, ask))
        first_time, first_bid, first_ask = quote_times[0]
        last_time = quote_times[-1][0]
        # a guaranteed pre-first-quote trade (as-of miss), integer shares
        trades.append((symbol_id, first_time - 1, (first_bid + first_ask) // 2, rng.randint(1, 5000)))
        for _ in range(trades_per_symbol):
            t = rng.randint(1, last_time)
            # the prevailing quote at t (backward), for a realistic price
            q = max((qt for qt in quote_times if qt[0] <= t), default=quote_times[0])
            _, bid, ask = q
            price = rng.randint(bid - (price_scale // 100), ask + (price_scale // 100))
            size = rng.randint(1, 5000)  # integer shares
            trades.append((symbol_id, t, price, size))
    quotes.sort(key=lambda r: (r[0], r[1]))
    trades.sort(key=lambda r: (r[0], r[1]))
    return MarketStream(quotes=tuple(quotes), trades=tuple(trades))

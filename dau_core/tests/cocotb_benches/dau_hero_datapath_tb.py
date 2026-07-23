from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import bars_from_field_aggregate, grouped_field_aggregate_rows, time_bucket_key_rows
from dau_core.temporal import (
    asof_backward,
    generate_market_stream,
    matched_rows,
    rolling_moments,
    signed_volume,
)

BUCKET_SHIFT = 3   # bar width 8
BUCKET_BITS = 16
WINDOW = 3

_STREAM = generate_market_stream(seed=4, num_symbols=3, quotes_per_symbol=24, trades_per_symbol=60)


def _merge(quotes, trades):
    tagged = [(0, s, t, bid, ask) for (s, t, bid, ask) in quotes]
    tagged += [(1, s, t, price, size) for (s, t, price, size) in trades]
    tagged.sort(key=lambda r: (r[1], r[2], r[0]))  # (symbol, time), quote before trade
    return tagged


def _golden(quotes, trades, *, tolerance=None):
    """The full workflow through the same generic stages the datapath composes:
    align → derive(signed_volume) → bucket+generic-aggregator → OHLCV select →
    roll over bar closes per symbol. Returns (symbol, count, total, total_sq)."""
    st = sorted(trades, key=lambda r: (r[0], r[1]))
    sq = sorted(quotes, key=lambda r: (r[0], r[1]))
    market = matched_rows(asof_backward(st, sq, tolerance=tolerance))
    bucket_rows = [(sym, price, time, size, signed_volume(price, size, bid, ask)) for sym, time, price, size, bid, ask in market]
    bars = bars_from_field_aggregate(grouped_field_aggregate_rows(time_bucket_key_rows(bucket_rows, bucket_shift=BUCKET_SHIFT, bucket_bits=BUCKET_BITS)))
    roll_input = [(bar.key >> BUCKET_BITS, bar.close) for bar in bars]
    moments = rolling_moments(roll_input, window=WINDOW)
    return [(sym, m.count, m.total, m.total_sq) for (sym, _v), m in zip(roll_input, moments) if m is not None]


@cocotb.test()
async def datapath_matches_golden(dut):
    await _start(dut)
    out = await _run(dut, _merge(_STREAM.quotes, _STREAM.trades))
    expected = _golden(_STREAM.quotes, _STREAM.trades)
    assert len(expected) > 0, "fixture must produce at least one rolling moment"
    assert out == expected


@cocotb.test()
async def datapath_backpressure(dut):
    await _start(dut)
    out = await _run(dut, _merge(_STREAM.quotes, _STREAM.trades), ready_pattern=(1, 0, 1, 0, 0))
    assert out == _golden(_STREAM.quotes, _STREAM.trades)


@cocotb.test()
async def datapath_with_tolerance(dut):
    await _start(dut)
    tol = 8
    out = await _run(dut, _merge(_STREAM.quotes, _STREAM.trades), tolerance=tol)
    assert out == _golden(_STREAM.quotes, _STREAM.trades, tolerance=tol)


@cocotb.test()
async def datapath_large_symbol_decodes_correctly(dut):
    await _start(dut)
    # symbol 50000 sets bit 31 of the composite key (50000 << BUCKET_BITS); the
    # partition must be extracted logically, not sign-extended
    quotes = [(50000, 1, 10, 12)]
    trades = [(50000, 2, 11, 1), (50000, 10, 13, 2), (50000, 18, 9, 1), (50000, 26, 15, 3), (50000, 34, 12, 2)]
    out = await _run(dut, _merge(quotes, trades))
    expected = _golden(quotes, trades)
    assert len(expected) > 0
    assert out == expected
    assert all(sym == 50000 for sym, *_ in out)  # decoded, not -15536


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    dut.cfg_has_tolerance.value = 0
    dut.cfg_tolerance.value = 0
    dut.cfg_bucket_shift.value = BUCKET_SHIFT
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, records, *, ready_pattern=(1,), tolerance=None):
    dut.cfg_has_tolerance.value = int(tolerance is not None)
    dut.cfg_tolerance.value = (tolerance if tolerance is not None else 0) & ((1 << 64) - 1)
    send = cocotb.start_soon(_send_records(dut, records))
    words = await _collect_output(dut, ready_pattern)
    await send
    count = await _accept_success_status(dut)
    assert len(words) % 3 == 0, "rolling-moment records are 3 words"
    assert count == len(words) // 3
    return [_decode(words[offset : offset + 3]) for offset in range(0, len(words), 3)]


def _decode(w):
    symbol = _s32(w[0] & 0xFFFFFFFF)
    count = w[0] >> 32
    return (symbol, count, _s64(w[1]), _s64(w[2]))


async def _send_records(dut, records) -> None:
    words = []
    for is_trade, symbol, time, a, b in records:
        words.append(((is_trade & 0xFFFFFFFF) << 32) | (symbol & 0xFFFFFFFF))
        words.append(time & ((1 << 64) - 1))
        words.append(((b & 0xFFFFFFFF) << 32) | (a & 0xFFFFFFFF))
    for index, word in enumerate(words):
        dut.input_data.value = word
        dut.input_last.value = int(index == len(words) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _collect_output(dut, ready_pattern):
    words = []
    pattern_index = 0
    for _ in range(65536):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        pattern_index += 1
        dut.output_ready.value = ready
        await ReadOnly()
        fired = ready == 1 and int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        status_pending = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                dut.output_ready.value = 0
                return words
        elif status_pending:
            dut.output_ready.value = 0
            return words
    raise AssertionError("timed out collecting datapath output")


async def _accept_success_status(dut) -> int:
    dut.status_ready.value = 1
    for _ in range(256):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            count = int(dut.moment_count.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return count
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


def _s32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


def _s64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.temporal import MarketFeatures, market_features

# (symbol, time, price, size, bid, ask)
_ROWS = [
    (570, 100, 12, 5, 10, 12),  # price at ask -> above mid -> +1
    (570, 101, 9, 3, 8, 12),  # below mid (10) -> -1
    (570, 102, 11, 7, 10, 12),  # at mid (11) -> 0
    (571, 100, 50, 4, 48, 52),  # mid 50, price 50 -> 0
    (571, 110, 55, 9, 50, 54),  # mid 52, price 55 -> +1
    (572, 5, -3, 2, -6, -2),  # negative prices; mid (-6-2)//2 = -4; price -3 > -4 -> +1
]


@cocotb.test()
async def feature_tile_matches_golden(dut):
    await _start(dut)
    feats = await _run(dut, _ROWS)
    assert feats == market_features(_ROWS)


@cocotb.test()
async def feature_tile_backpressure_both_sides(dut):
    await _start(dut)
    feats = await _run(dut, _ROWS, ready_pattern=(1, 0, 0, 1))
    assert feats == market_features(_ROWS)


@cocotb.test()
async def feature_tile_int64_dollar_volume(dut):
    await _start(dut)
    rows = [(3, 1, 2**30, 5000, 2**30 - 10, 2**30 + 10)]
    feats = await _run(dut, rows)
    assert feats == market_features(rows)
    assert feats[0].dollar_volume == 2**30 * 5000


@cocotb.test()
async def feature_tile_wide_spread_and_midpoint_do_not_overflow(dut):
    await _start(dut)
    # opposite-sign quotes: spread = ask-bid = 2**32-1 exceeds int32; midpoint
    # sum also exceeds int32 — both must be exact vs the arbitrary-precision golden
    rows = [(4, 7, 0, 1, -(2**31), 2**31 - 1)]
    feats = await _run(dut, rows)
    assert feats == market_features(rows)
    assert feats[0].spread == 2**32 - 1
    assert feats[0].midpoint == -1  # (-2**31 + 2**31-1)//2 = -1//2 = -1


@cocotb.test()
async def feature_tile_single_row(dut):
    await _start(dut)
    assert await _run(dut, [(9, 42, 7, 3, 4, 8)]) == market_features([(9, 42, 7, 3, 4, 8)])


@cocotb.test()
async def feature_tile_truncated_row_reports_stream_error(dut):
    await _start(dut)
    dut.output_ready.value = 1
    dut.input_data.value = 100  # r0 (time) with last asserted: a torn row
    dut.input_last.value = 1
    dut.input_valid.value = 1
    while True:
        await ReadOnly()
        fired = int(dut.input_ready.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            break
    dut.input_valid.value = 0
    dut.input_last.value = 0
    await _expect_error(dut)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, *, ready_pattern=(1,)):
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_output(dut, ready_pattern)
    await send
    count = await _accept_success_status(dut)
    assert len(words) % 7 == 0, "feature records are 7 words"
    assert count == len(words) // 7
    return [_decode(words[offset : offset + 7]) for offset in range(0, len(words), 7)]


def _decode(words) -> MarketFeatures:
    time = _s64(words[0])
    symbol = _s32(words[1] & 0xFFFFFFFF)
    price = _s32(words[1] >> 32)
    midpoint = _s32(words[2] & 0xFFFFFFFF)
    trade_sign = _s32(words[2] >> 32)
    size = _s32(words[3] & 0xFFFFFFFF)
    spread = _s64(words[4])
    signed_volume = _s64(words[5])
    dollar_volume = _s64(words[6])
    return MarketFeatures(
        symbol_id=symbol,
        time=time,
        price=price,
        size=size,
        midpoint=midpoint,
        spread=spread,
        trade_sign=trade_sign,
        signed_volume=signed_volume,
        dollar_volume=dollar_volume,
    )


async def _send_rows(dut, rows) -> None:
    words = []
    for symbol, time, price, size, bid, ask in rows:
        words.append(time & ((1 << 64) - 1))
        words.append(((price & 0xFFFFFFFF) << 32) | (symbol & 0xFFFFFFFF))
        words.append(((ask & 0xFFFFFFFF) << 32) | (bid & 0xFFFFFFFF))
        words.append(size & 0xFFFFFFFF)
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
    for pattern_index in range(8192):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        dut.output_ready.value = ready
        await ReadOnly()
        fired = ready == 1 and int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                dut.output_ready.value = 0
                return words
    raise AssertionError("timed out collecting feature output")


async def _accept_success_status(dut) -> int:
    dut.status_ready.value = 1
    for _ in range(128):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            count = int(dut.feature_count.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return count
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


async def _expect_error(dut) -> None:
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == 2
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for error status")


def _s32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


def _s64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value

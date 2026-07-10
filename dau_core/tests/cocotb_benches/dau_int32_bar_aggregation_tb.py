from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import BarAggregate, GroupedAggregationErrorCode, bar_aggregate_rows

_TRADES = [
    (570, 100, 10, 500_000),
    (570, 105, 3, 0),
    (570, 95, 2, 250_000),
    (570, 102, 1, 999_999),
    (571, 102, 7, 0),
    (571, 99, 4, 100_000),
    (572, -3, 0, 1),
]


@cocotb.test()
async def bar_tile_matches_golden(dut):
    await _start(dut)
    bars = await _run_bars(dut, _TRADES)
    assert bars == bar_aggregate_rows(_TRADES)


@cocotb.test()
async def bar_tile_backpressure_both_sides(dut):
    await _start(dut)
    bars = await _run_bars(dut, _TRADES, ready_pattern=(1, 0, 0, 1, 0))
    assert bars == bar_aggregate_rows(_TRADES)


@cocotb.test()
async def bar_tile_single_row_and_reappearing_key(dut):
    await _start(dut)
    assert await _run_bars(dut, [(9, -42, 5, 0)]) == [BarAggregate(key=9, open=-42, close=-42, low=-42, high=-42, shares=5, frac=0, count=1)]
    rows = [(5, 1, 1, 0), (7, 2, 2, 0), (5, 3, 3, 0)]
    bars = await _run_bars(dut, rows)
    assert [bar.key for bar in bars] == [5, 7, 5]
    assert bars == bar_aggregate_rows(rows)


@cocotb.test()
async def bar_tile_key_change_on_final_row(dut):
    await _start(dut)
    rows = [(1, 10, 1, 0), (1, 20, 2, 0), (2, 30, 3, 5)]
    assert await _run_bars(dut, rows) == bar_aggregate_rows(rows)


@cocotb.test()
async def bar_tile_int64_sums(dut):
    await _start(dut)
    rows = [(3, 1, 2**31 - 1, 999_999)] * 5
    bars = await _run_bars(dut, rows)
    assert bars[0].shares == 5 * (2**31 - 1)
    assert bars[0].frac == 5 * 999_999


@cocotb.test()
async def bar_tile_repeated_batches_reset(dut):
    await _start(dut)
    assert await _run_bars(dut, _TRADES) == bar_aggregate_rows(_TRADES)
    second = [(572, 100, 1, 0)]  # same key as previous tail: fresh bar
    assert await _run_bars(dut, second) == [BarAggregate(key=572, open=100, close=100, low=100, high=100, shares=1, frac=0, count=1)]


@cocotb.test()
async def bar_tile_reserved_mode_reports_config_error(dut):
    await _start(dut)
    _apply_config(dut, row_count=1)
    dut.cfg_mode.value = 1
    dut.output_ready.value = 1
    await _send_rows(dut, [(1, 2, 3, 4)])
    await _expect_error(dut, GroupedAggregationErrorCode.CONFIG)


@cocotb.test()
async def bar_tile_truncated_row_reports_stream_error(dut):
    await _start(dut)
    _apply_config(dut, row_count=1)
    dut.output_ready.value = 1
    # input_last on w0: a torn row
    dut.input_data.value = (100 << 32) | 570
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
    await _expect_error(dut, GroupedAggregationErrorCode.STREAM)


@cocotb.test()
async def bar_tile_short_stream_reports_stream_error(dut):
    await _start(dut)
    _apply_config(dut, row_count=4)
    dut.output_ready.value = 1
    await _send_rows(dut, [(1, 2, 3, 4), (1, 5, 6, 7)])  # last after 2 of 4 rows
    await _expect_error(dut, GroupedAggregationErrorCode.STREAM)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    dut.cfg_mode.value = 0
    dut.cfg_row_count.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


def _apply_config(dut, *, row_count: int) -> None:
    dut.cfg_mode.value = 0
    dut.cfg_row_count.value = row_count


async def _run_bars(dut, rows, *, ready_pattern=(1,)):
    _apply_config(dut, row_count=len(rows))
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_output(dut, ready_pattern)
    await send
    count = await _accept_success_status(dut)
    assert len(words) % 6 == 0, "bar records are 6 words"
    assert count == len(words) // 6
    return [_decode_bar(words[offset : offset + 6]) for offset in range(0, len(words), 6)]


def _decode_bar(words: list[int]) -> BarAggregate:
    return BarAggregate(
        key=_signed32(words[0] & 0xFFFFFFFF),
        open=_signed32(words[0] >> 32),
        close=_signed32(words[1] & 0xFFFFFFFF),
        low=_signed32(words[1] >> 32),
        high=_signed32(words[2] & 0xFFFFFFFF),
        shares=_signed64(words[3]),
        frac=_signed64(words[4]),
        count=words[5],
    )


async def _send_rows(dut, rows) -> None:
    words = []
    for key, price, shares, frac in rows:
        words.append(((price & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF))
        words.append(((frac & 0xFFFFFFFF) << 32) | (shares & 0xFFFFFFFF))
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


async def _collect_output(dut, ready_pattern) -> list[int]:
    words: list[int] = []
    pattern_index = 0
    for _ in range(4096):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        pattern_index += 1
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
    raise AssertionError("timed out collecting bar output")


async def _accept_success_status(dut) -> int:
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            count = int(dut.bar_count.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return count
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


async def _expect_error(dut, code) -> None:
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == int(code)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for error status")


def _signed32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


def _signed64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value

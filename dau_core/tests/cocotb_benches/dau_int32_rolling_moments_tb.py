from __future__ import annotations

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.temporal import rolling_moments, rolling_sum

WINDOW = int(os.environ.get("DAU_ROLLING_WINDOW", "4"))

# (symbol, value): symbols grouped and contiguous (the host pre-sort precondition)
_VALUES = [
    (7, 10),
    (7, 20),
    (7, 30),
    (7, 40),
    (7, 50),
    (7, 60),  # slides once full
    (9, -5),
    (9, -5),
    (9, 100),  # < WINDOW: all warm-up
    (11, 3),
    (11, 1),
    (11, 4),
    (11, 1),
    (11, 5),
    (11, 9),
]


def _expected(values, *, window=WINDOW):
    moments = rolling_moments(values, window=window)
    return [(symbol, window, m.total, m.total_sq) for (symbol, _value), m in zip(values, moments) if m is not None]


@cocotb.test()
async def rolling_moments_matches_golden(dut):
    await _start(dut)
    rows = await _run(dut, _VALUES)
    assert rows == _expected(_VALUES)


@cocotb.test()
async def rolling_moments_backpressure_both_sides(dut):
    await _start(dut)
    rows = await _run(dut, _VALUES, ready_pattern=(1, 0, 0, 1, 0))
    assert rows == _expected(_VALUES)


@cocotb.test()
async def rolling_moments_total_field_is_rolling_sum(dut):
    await _start(dut)
    rows = await _run(dut, _VALUES)
    sums = [total for (_s, _c, total, _sq) in rows]
    golden_sums = [s for s in rolling_sum(_VALUES, window=WINDOW) if s is not None]
    assert sums == golden_sums


@cocotb.test()
async def rolling_moments_all_warmup_emits_nothing(dut):
    await _start(dut)
    short = [(3, 1)] * (WINDOW - 1)  # never fills the window
    rows = await _run(dut, short)
    assert rows == []
    assert _expected(short) == []


@cocotb.test()
async def rolling_moments_int64_sum_of_squares(dut):
    await _start(dut)
    big = 2**30
    vals = [(5, big)] * WINDOW
    rows = await _run(dut, vals)
    assert rows == _expected(vals)
    assert rows[-1] == (5, WINDOW, WINDOW * big, WINDOW * big * big)


@cocotb.test()
async def rolling_moments_output_last_on_final_after_warmup_tail(dut):
    await _start(dut)
    # a full window on symbol 7, then a single fresh symbol at the end (warm-up,
    # no emit): output_last must ride the buffered last full-window record
    vals = [(7, v) for v in range(1, WINDOW + 2)] + [(8, 99)]
    dut.output_ready.value = 0
    send = cocotb.start_soon(_send_rows(dut, vals))
    words, last_at = await _collect_with_last(dut)
    await send
    count = await _accept_success_status(dut)
    rows = [_decode(words[o : o + 3]) for o in range(0, len(words), 3)]
    assert rows == _expected(vals)
    assert count == len(rows)
    assert last_at == len(words) - 1, (last_at, len(words))


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


async def _run(dut, values, *, ready_pattern=(1,)):
    send = cocotb.start_soon(_send_rows(dut, values))
    words = await _collect_output(dut, ready_pattern)
    await send
    count = await _accept_success_status(dut)
    assert len(words) % 3 == 0, "moment records are 3 words"
    assert count == len(words) // 3
    return [_decode(words[offset : offset + 3]) for offset in range(0, len(words), 3)]


def _decode(words):
    symbol = _signed32(words[0] & 0xFFFFFFFF)
    count = words[0] >> 32
    total = _signed64(words[1])
    total_sq = _signed64(words[2])
    return (symbol, count, total, total_sq)


async def _send_rows(dut, values) -> None:
    for index, (symbol, value) in enumerate(values):
        dut.input_data.value = ((value & 0xFFFFFFFF) << 32) | (symbol & 0xFFFFFFFF)
        dut.input_last.value = int(index == len(values) - 1)
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
    raise AssertionError("timed out collecting rolling-moment output")


async def _collect_with_last(dut):
    words = []
    last_at = None
    for _ in range(8192):
        dut.output_ready.value = 1
        await ReadOnly()
        fired = int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        status_pending = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                last_at = len(words) - 1
                dut.output_ready.value = 0
                return words, last_at
        elif status_pending:
            dut.output_ready.value = 0
            return words, last_at
    raise AssertionError("timed out collecting rolling-moment output")


async def _accept_success_status(dut) -> int:
    dut.status_ready.value = 1
    for _ in range(128):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            count = int(dut.moment_count.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return count
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


def _signed32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


def _signed64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value

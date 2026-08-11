from __future__ import annotations

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.temporal import rolling_max, rolling_min

WINDOW = int(os.environ.get("DAU_EXTREMUM_WINDOW", "4"))
VALUE_WIDTH = int(os.environ.get("DAU_EXTREMUM_VALUE_WIDTH", "32"))
FIND_MAX = int(os.environ.get("DAU_EXTREMUM_FIND_MAX", "1"))
WORDS = 2 if VALUE_WIDTH == 64 else 1

# (symbol, value): symbols grouped and contiguous — the host pre-sort
# precondition the segmented network relies on for its per-symbol reset.
_VALUES = [
    (7, 10),
    (7, 40),
    (7, 30),
    (7, 20),
    (7, 5),  # slides: the 40 leaves the window here at WINDOW=4
    (7, 60),
    (9, -5),
    (9, -9),
    (9, 100),  # short group at WINDOW=4: all warm-up
    (11, 3),
    (11, 1),
    (11, 4),
    (11, 1),
    (11, 5),
    (11, 9),
]


def _golden(values, *, window=WINDOW):
    rolling = rolling_max if FIND_MAX else rolling_min
    result = rolling(values, window=window)
    return [(symbol, extremum) for (symbol, _value), extremum in zip(values, result) if extremum is not None]


@cocotb.test()
async def rolling_extremum_matches_golden(dut):
    await _start(dut)
    assert await _run(dut, _VALUES) == _golden(_VALUES)


@cocotb.test()
async def rolling_extremum_backpressure_both_sides(dut):
    """Backpressure must not change a value or reorder a row: the network is
    a pipeline, and a skid slot sits between it and the output."""
    await _start(dut)
    assert await _run(dut, _VALUES, ready_pattern=(1, 0, 0, 1, 0)) == _golden(_VALUES)


@cocotb.test()
async def rolling_extremum_resets_per_symbol(dut):
    """The segment boundary is the whole point: a new symbol must not see the
    previous symbol's values, however extreme they were."""
    await _start(dut)
    values = [(1, 10**6)] * WINDOW + [(2, -3)] * WINDOW
    rows = await _run(dut, values)
    assert rows == _golden(values)
    tail = [extremum for symbol, extremum in rows if symbol == 2]
    assert tail and all(value == -3 for value in tail), tail


@cocotb.test()
async def rolling_extremum_all_warmup_emits_nothing_and_closes_out(dut):
    """A stream that forwards NOTHING still closes out. Zero forwarded rows is
    the one outcome a downstream tile cannot tell apart from a stall, which is
    why this tile is silent on success only while it is forwarding."""
    await _start(dut)
    short = [(3, 1)] * (WINDOW - 1)  # never fills the window
    rows = await _run(dut, short, expect_close_out=True)
    assert rows == []
    assert _golden(short) == []


@cocotb.test()
async def rolling_extremum_last_rides_the_final_forwarded_row(dut):
    """The final input rows may all be warm-up. output_last still has to land
    on the last row actually forwarded, so the skid holds it back until the
    stream ends."""
    await _start(dut)
    values = [(7, v) for v in range(1, WINDOW + 1)] + [(8, 99)]  # trailing group is warm-up
    words, last_at = await _collect_with_last(dut, values)
    rows = _decode_rows(words)
    assert rows == _golden(values)
    assert last_at == len(words) - 1, (last_at, len(words))


@cocotb.test()
async def rolling_extremum_reuses_across_batches(dut):
    """A SECOND batch after input_last must be a fresh window.

    Every other test resets first, which is exactly how a tile that carries
    its segment state across a batch boundary passes a whole bench. Here the
    second batch opens with the SAME symbol the first one closed on: if the
    run length survived, its first rows emit during what should be warm-up.
    """
    await _start(dut)
    batch = [(7, v) for v in range(1, WINDOW + 1)]
    first = await _run(dut, batch)
    assert first == _golden(batch)
    # no reset between batches
    second = await _run(dut, batch)
    assert second == _golden(batch), "the second batch inherited the first batch's window"


@cocotb.test()
async def rolling_extremum_zero_row_close_out_fires_exactly_once(dut):
    """The close-out is a pulse, not a level.

    The condition is derived from a latched 'stream has ended' bit, so
    without a fired-once guard it re-asserts every cycle and a consumer
    holding status_ready sees an unbounded run of transfers.
    """
    await _start(dut)
    short = [(3, 1)] * (WINDOW - 1)
    rows = await _run(dut, short, expect_close_out=True)
    assert rows == []
    # status must be quiescent afterwards
    dut.status_ready.value = 1
    extra = 0
    for _ in range(64):
        await ReadOnly()
        if int(dut.status_valid.value) == 1:
            extra += 1
        await RisingEdge(dut.clk)
    dut.status_ready.value = 0
    assert extra == 0, f"close-out re-fired {extra} more times after being accepted"


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


async def _run(dut, values, *, ready_pattern=(1,), expect_close_out=False):
    send = cocotb.start_soon(_send_rows(dut, values))
    words = await _collect_output(dut, ready_pattern)
    await send
    if expect_close_out:
        await _accept_success_status(dut)
    return _decode_rows(words)


def _decode_rows(words):
    rows = []
    for offset in range(0, len(words), WORDS):
        chunk = words[offset : offset + WORDS]
        symbol = _signed32(chunk[0] & 0xFFFFFFFF)
        low = chunk[0] >> 32
        value = _signed(low | ((chunk[1] & 0xFFFFFFFF) << 32), 64) if WORDS == 2 else _signed(low, 32)
        rows.append((symbol, value))
    return rows


async def _send_rows(dut, values) -> None:
    for index, (symbol, value) in enumerate(values):
        final = index == len(values) - 1
        masked = value & ((1 << VALUE_WIDTH) - 1)
        words = [((masked & 0xFFFFFFFF) << 32) | (symbol & 0xFFFFFFFF)]
        if WORDS == 2:
            words.append((masked >> 32) & 0xFFFFFFFF)
        for word_index, word in enumerate(words):
            dut.input_data.value = word
            dut.input_last.value = int(final and word_index == len(words) - 1)
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
    for index in range(8192):
        ready = ready_pattern[index % len(ready_pattern)]
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
    raise AssertionError("timed out collecting rolling-extremum output")


async def _collect_with_last(dut, values):
    send = cocotb.start_soon(_send_rows(dut, values))
    words = []
    last_at = None
    for _ in range(8192):
        dut.output_ready.value = 1
        await ReadOnly()
        fired = int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                last_at = len(words) - 1
                dut.output_ready.value = 0
                break
    await send
    return words, last_at


async def _accept_success_status(dut) -> None:
    dut.status_ready.value = 1
    for _ in range(256):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0, "an empty stream closes out CLEAN, not as an error"
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for the zero-row close-out")


def _signed(value: int, width: int) -> int:
    value &= (1 << width) - 1
    return value - (1 << width) if value >> (width - 1) else value


def _signed32(value: int) -> int:
    return _signed(value, 32)

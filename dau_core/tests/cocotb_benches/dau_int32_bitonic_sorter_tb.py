from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import SorterErrorCode, sort_bar_rows_by_key

# must match the DUT's CAPACITY parameter (the pytest runner sets the env var
# for parameterized builds)
CAPACITY = int(os.environ.get("DAU_SORTER_CAPACITY", "16"))
_MASK32 = 0xFFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# (key, price, shares, frac)
_ROWS_FULL = [
    (25, 95, 2, 250_000),
    (5, 100, 10, 500_000),
    (35, 102, 1, 999_999),
    (10, 105, 3, 0),
    (-3, 88, 9, 1),
    (30, 99, 4, 100_000),
    (19, 102, 7, 0),
]
# trimmed to the DUT capacity so the same vectors run at small CAPACITY builds
_ROWS = _ROWS_FULL[:CAPACITY]


@cocotb.test()
async def sorts_rows_ascending_by_key(dut):
    await _start(dut)
    assert await _run(dut, _ROWS) == sort_bar_rows_by_key(_ROWS)


@cocotb.test()
async def already_sorted_batch_passes_through(dut):
    await _start(dut)
    rows = sort_bar_rows_by_key(_ROWS)
    assert await _run(dut, rows) == rows


@cocotb.test()
async def reverse_order_batch(dut):
    await _start(dut)
    rows = list(reversed(sort_bar_rows_by_key(_ROWS)))
    assert await _run(dut, rows) == sort_bar_rows_by_key(rows)


@cocotb.test()
async def duplicate_keys_drain_stable(dut):
    # equal keys keep input order (the load-index tie-break): payloads verify it
    await _start(dut)
    rows = [(7, 1, 1, 0), (3, 2, 2, 0), (7, 3, 3, 0), (3, 4, 4, 0)][:CAPACITY]
    out = await _run(dut, rows)
    assert out == sort_bar_rows_by_key(rows)
    if CAPACITY >= 4:
        assert out == [(3, 2, 2, 0), (3, 4, 4, 0), (7, 1, 1, 0), (7, 3, 3, 0)]


@cocotb.test()
async def int32_extreme_keys_sort_signed(dut):
    # INT32_MAX keys are real rows and must not be confused with the +max pad
    # sentinels; INT32_MIN sorts first (signed compare)
    await _start(dut)
    rows = [(_INT32_MAX, 1, 1, 0), (_INT32_MIN, 3, 3, 0), (_INT32_MAX, 4, 4, 0), (-1, 5, 5, 0)][:CAPACITY]
    out = await _run(dut, rows)
    assert out == sort_bar_rows_by_key(rows)
    assert out[0] == (_INT32_MIN, 3, 3, 0)  # signed: INT32_MIN first
    if CAPACITY >= 4:
        assert out[-2:] == [(_INT32_MAX, 1, 1, 0), (_INT32_MAX, 4, 4, 0)]  # stable


@cocotb.test()
async def all_equal_full_batch_is_stable(dut):
    # the strongest stability case: a full batch of one key must drain in
    # exactly input order (any compare-exchange on equal keys would reorder)
    await _start(dut)
    rows = [(7, i, i + 1, i + 2) for i in range(CAPACITY)]
    assert await _run(dut, rows) == rows


@cocotb.test()
async def int32_max_partial_batch_beats_pad_sentinels(dut):
    # a PARTIAL batch of real INT32_MAX keys: the unused entries are padded
    # with the same +max key, so only the index tie-break (pad idx >= count)
    # keeps the pads after the real rows — all rows must survive, in order
    await _start(dut)
    rows = [(_INT32_MAX, i, i, 0) for i in range(max(1, CAPACITY - 1))]
    assert await _run(dut, rows) == rows


@cocotb.test()
async def single_row_batch(dut):
    await _start(dut)
    assert await _run(dut, [(42, 1, 2, 3)]) == [(42, 1, 2, 3)]


@cocotb.test()
async def exact_capacity_batch(dut):
    await _start(dut)
    rows = [(CAPACITY - i, i, i, 0) for i in range(CAPACITY)]
    assert await _run(dut, rows) == sort_bar_rows_by_key(rows)


@cocotb.test()
async def overflow_reports_capacity_error(dut):
    # CAPACITY+1 rows: the batch is consumed to its end and reported as
    # ERR_CAPACITY (partition first); nothing is emitted; the sorter recovers
    await _start(dut)
    rows = [(i, i, i, 0) for i in range(CAPACITY + 1)]
    send = cocotb.start_soon(_send_rows(dut, rows))
    await _expect_status(dut, error=True, code=SorterErrorCode.CAPACITY)
    await send
    assert await _run(dut, _ROWS) == sort_bar_rows_by_key(_ROWS)


@cocotb.test()
async def torn_row_reports_stream_error(dut):
    await _start(dut)
    # input_last on a w0: a torn row
    dut.input_data.value = (100 << 32) | 5
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
    await _expect_status(dut, error=True, code=SorterErrorCode.STREAM)

    # recovers: the next batch sorts normally
    assert await _run(dut, _ROWS) == sort_bar_rows_by_key(_ROWS)


@cocotb.test()
async def torn_w0_after_full_batch_is_stream_error(dut):
    # precedence at the capacity boundary: a lone last-carrying w0 after
    # CAPACITY complete rows is a torn row (ERR_STREAM, the universal rule),
    # not an overflow
    await _start(dut)
    rows = [(i, i, i, 0) for i in range(CAPACITY)]
    send = cocotb.start_soon(_send_rows_then_torn_w0(dut, rows))
    await _expect_status(dut, error=True, code=SorterErrorCode.STREAM)
    await send
    assert await _run(dut, _ROWS) == sort_bar_rows_by_key(_ROWS)


@cocotb.test()
async def error_status_survives_backpressure(dut):
    # while status_ready is held low the error close-out must stay stable and
    # the sorter must refuse new input; recovery only after the handshake
    await _start(dut)
    dut.status_ready.value = 0
    dut.input_data.value = (100 << 32) | 5
    dut.input_last.value = 1  # torn row
    dut.input_valid.value = 1
    while True:
        await ReadOnly()
        fired = int(dut.input_ready.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            break
    dut.input_valid.value = 0
    dut.input_last.value = 0

    found = False
    for _ in range(8):
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if found:
            break
    assert found, "error status never asserted"
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "error status not held"
        assert int(dut.status_error.value) == 1
        assert int(dut.status_error_code.value) == SorterErrorCode.STREAM
        assert int(dut.input_ready.value) == 0, "sorter accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=True, code=SorterErrorCode.STREAM)
    assert await _run(dut, _ROWS) == sort_bar_rows_by_key(_ROWS)


@cocotb.test()
async def backpressure_on_the_output(dut):
    await _start(dut)
    out = await _run(dut, _ROWS, ready_pattern=(1, 0, 0, 1, 0))
    assert out == sort_bar_rows_by_key(_ROWS)


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0xB170)
    for trial in range(40):
        count = rng.randint(1, CAPACITY)
        rows = []
        for _ in range(count):
            kind = rng.random()
            if kind < 0.1:
                key = rng.choice((_INT32_MIN, _INT32_MAX, 0, -1))
            else:
                key = rng.randint(-9, 9)  # small range -> many duplicates
            rows.append((key, rng.randint(-1000, 1000), rng.randint(0, 1000), rng.randint(0, 1_000_000)))
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):  # a ready pattern must let data through eventually
            pattern = pattern + (1,)
        out = await _run(dut, rows, ready_pattern=pattern)
        assert out == sort_bar_rows_by_key(rows), f"trial {trial}: rows={rows}"


# ---- helpers ----


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


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
    words = await _collect_output(dut, ready_pattern, expect_words=2 * len(rows))
    await send
    assert int(dut.sorted_count.value) == len(rows), "sorted_count != rows drained"
    out = []
    for offset in range(0, len(words), 2):
        w0, w1 = words[offset], words[offset + 1]
        out.append((_signed32(w0 & _MASK32), _signed32(w0 >> 32), _signed32(w1 & _MASK32), _signed32(w1 >> 32)))
    return out


async def _send_rows_then_torn_w0(dut, rows) -> None:
    """Send complete rows (no last), then a lone last-carrying w0."""
    words = []
    for key, price, shares, frac in rows:
        words.append(((price & _MASK32) << 32) | (key & _MASK32))
        words.append(((frac & _MASK32) << 32) | (shares & _MASK32))
    words.append((999 << 32) | 999)  # the torn w0
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


async def _send_rows(dut, rows) -> None:
    words = []
    for key, price, shares, frac in rows:
        words.append(((price & _MASK32) << 32) | (key & _MASK32))
        words.append(((frac & _MASK32) << 32) | (shares & _MASK32))
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


async def _collect_output(dut, ready_pattern, *, expect_words: int) -> list[int]:
    words: list[int] = []
    pattern_index = 0
    for _ in range(200000):
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
                assert len(words) == expect_words, f"expected {expect_words} words, got {len(words)}"
                # the drain ends with a success close-out status (the record
                # writer's done waits on a consumed producer status)
                await _expect_status(dut, error=False)
                await _assert_quiescent(dut)
                return words
    raise AssertionError(f"timed out collecting sorter output ({len(words)}/{expect_words} words)")


async def _assert_quiescent(dut) -> None:
    """After a batch drains, nothing more may appear on the output or status
    streams even with ready held high (no trailing/duplicated beats)."""
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    for _ in range(6):
        await ReadOnly()
        assert int(dut.output_valid.value) == 0, "output beat after batch completion"
        assert int(dut.status_valid.value) == 0, "status beat after batch completion"
        await RisingEdge(dut.clk)
    dut.output_ready.value = 0
    dut.status_ready.value = 0


async def _expect_status(dut, *, error: bool, code: int = 0) -> None:
    dut.status_ready.value = 1
    for _ in range(4096):
        await ReadOnly()
        fired = int(dut.status_valid.value) == 1
        observed_error = int(dut.status_error.value)
        observed_code = int(dut.status_error_code.value)
        await RisingEdge(dut.clk)
        if fired:
            assert observed_error == int(error)
            if error:
                assert observed_code == code, f"status code {observed_code} != {code}"
            else:
                assert observed_code == 0, f"success close-out carried code {observed_code}"
            dut.status_ready.value = 0
            return
    raise AssertionError("timed out waiting for sorter status")

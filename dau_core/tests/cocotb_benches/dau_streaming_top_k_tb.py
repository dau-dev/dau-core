from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import streaming_top_k

K = int(os.environ.get("DAU_TOP_K", "8"))

# (key, v0, v1, v2); payloads are arrival-derived so a mis-routed row is caught
_ROWS = [
    (30, 0, 100, 200),
    (90, 1, 101, 201),
    (10, 2, 102, 202),
    (70, 3, 103, 203),
    (50, 4, 104, 204),
    (90, 5, 105, 205),  # tie at 90 (later than arrival 1)
    (20, 6, 106, 206),
    (90, 7, 107, 207),  # tie at 90
    (60, 8, 108, 208),
    (40, 9, 109, 209),
    (80, 10, 110, 210),
    (90, 11, 111, 211),  # tie at 90 (latest) — dropped once four 90s are held at K>=... depends on K
]


@cocotb.test()
async def top_k_matches_golden(dut):
    await _start(dut)
    rows = await _run(dut, _ROWS)
    assert rows == streaming_top_k(_ROWS, k=K)


@cocotb.test()
async def top_k_backpressure(dut):
    await _start(dut)
    rows = await _run(dut, _ROWS, ready_pattern=(1, 0, 0, 1, 0))
    assert rows == streaming_top_k(_ROWS, k=K)


@cocotb.test()
async def top_k_ties_keep_earliest_arrival(dut):
    await _start(dut)
    # more rows tied at the top than K: only the earliest K survive, in order
    tied = [(5, i, 0, 0) for i in range(K + 3)]
    rows = await _run(dut, tied)
    assert rows == streaming_top_k(tied, k=K)
    assert rows == [(5, i, 0, 0) for i in range(K)]


@cocotb.test()
async def top_k_signed_key_comparison(dut):
    await _start(dut)
    rows_in = [
        (-1, 0, 0, 0),
        (1, 1, 0, 0),
        (-(2**31), 2, 0, 0),
        (2**31 - 1, 3, 0, 0),
        (0, 4, 0, 0),
    ]
    rows = await _run(dut, rows_in)
    assert rows == streaming_top_k(rows_in, k=K)


@cocotb.test()
async def top_k_fewer_rows_than_k(dut):
    await _start(dut)
    few = [(7, 0, 0, 0), (3, 1, 0, 0)]  # 2 rows; both kept when K >= 2
    rows = await _run(dut, few)
    assert rows == streaming_top_k(few, k=K)
    if K >= 2:
        assert rows == [(7, 0, 0, 0), (3, 1, 0, 0)]


@cocotb.test()
async def top_k_random_streams(dut):
    await _start(dut)
    rng = random.Random(K * 7 + 1)
    for _ in range(6):
        n = rng.randint(1, 30)
        rows_in = [(rng.randint(-40, 40), i, rng.randint(0, 50), rng.randint(0, 50)) for i in range(n)]
        rows = await _run(dut, rows_in)
        assert rows == streaming_top_k(rows_in, k=K), (K, n, rows_in)


@cocotb.test()
async def top_k_truncated_row_reports_stream_error(dut):
    await _start(dut)
    dut.output_ready.value = 1
    # input_last on w0 (the key word): a torn one-word row
    dut.input_data.value = 5
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
    assert len(words) % 2 == 0, "top-k rows are two words"
    assert count == len(words) // 2
    return [_decode_row(words[offset], words[offset + 1]) for offset in range(0, len(words), 2)]


def _decode_row(w0, w1):
    key = _signed32(w0 & 0xFFFFFFFF)
    v0 = _signed32(w0 >> 32)
    v1 = _signed32(w1 & 0xFFFFFFFF)
    v2 = _signed32(w1 >> 32)
    return (key, v0, v1, v2)


async def _send_rows(dut, rows) -> None:
    words = []
    for key, v0, v1, v2 in rows:
        words.append(((v0 & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF))
        words.append(((v2 & 0xFFFFFFFF) << 32) | (v1 & 0xFFFFFFFF))
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
    raise AssertionError("timed out collecting top-k output")


async def _accept_success_status(dut) -> int:
    dut.status_ready.value = 1
    for _ in range(128):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            count = int(dut.top_count.value)
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


def _signed32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value

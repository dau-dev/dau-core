from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import SessionErrorCode, sessionize_scan

CAPACITY = int(os.environ.get("DAU_SESSION_GAP_CAPACITY", "1024"))

# time-ordered, interleaved keys: gap splits, extensions, a batch-end drain
_ROWS = [(1, 0), (2, 1), (2, 2), (1, 5), (1, 20), (3, 21)]


@cocotb.test()
async def session_gap_matches_golden(dut):
    await _start(dut)
    got, code = await _run(dut, _ROWS, gap=3, granted_keys=8)
    expected, expected_code = sessionize_scan(_ROWS, gap=3, capacity=8)
    assert code == expected_code
    assert got == expected


@cocotb.test()
async def session_gap_boundary_is_exclusive(dut):
    await _start(dut)
    extended, code = await _run(dut, [(1, 0), (1, 3)], gap=3, granted_keys=4)
    assert code is SessionErrorCode.OK and extended == [(1, 0, 3, 2)]
    split, code = await _run(dut, [(1, 0), (1, 4)], gap=3, granted_keys=4)
    assert code is SessionErrorCode.OK and split == [(1, 0, 0, 1), (1, 4, 4, 1)]


@cocotb.test()
async def session_gap_drain_is_key_sorted(dut):
    # keys arrive in DESCENDING order; the drain must emit ascending — the
    # implementation-independent contract, including a negative key first
    await _start(dut)
    rows = [(9, 0), (4, 1), (-3, 2)]
    got, code = await _run(dut, rows, gap=100, granted_keys=8)
    expected, expected_code = sessionize_scan(rows, gap=100, capacity=8)
    assert code == expected_code
    assert got == expected == [(-3, 2, 2, 1), (4, 1, 1, 1), (9, 0, 0, 1)]


@cocotb.test()
async def session_gap_time_order_violation_is_refused(dut):
    await _start(dut)
    rows = [(1, 0), (1, 10), (1, 5)]  # 5 < 10: refused, never reordered
    got, code = await _run(dut, rows, gap=3, granted_keys=4)
    expected, expected_code = sessionize_scan(rows, gap=3, capacity=4)
    assert expected_code is SessionErrorCode.TIME_ORDER
    assert code == expected_code
    assert got == expected == [(1, 0, 0, 1)]  # the gap-closed session survives


@cocotb.test()
async def session_gap_capacity_bounds_distinct_keys(dut):
    await _start(dut)
    rows = [(1, 0), (2, 0), (3, 0)]
    got, code = await _run(dut, rows, gap=3, granted_keys=2)
    assert code is SessionErrorCode.SESSION_CAPACITY_EXCEEDED
    assert got == []


@cocotb.test()
async def session_gap_bad_parameters_are_refused(dut):
    await _start(dut)
    got, code = await _run(dut, [(1, 0), (1, 1)], gap=0, granted_keys=4)
    assert code is SessionErrorCode.BAD_GAP and got == []
    got, code = await _run(dut, [(1, 0), (1, 1)], gap=-1, granted_keys=4)
    assert code is SessionErrorCode.BAD_GAP and got == []
    got, code = await _run(dut, [(1, 0), (1, 1)], gap=3, granted_keys=CAPACITY + 1)
    assert code is SessionErrorCode.BAD_CAPACITY and got == []


@cocotb.test()
async def session_gap_colliding_keys_probe_correctly(dut):
    await _start(dut)
    rows = sorted([(0, 0), (CAPACITY, 1), (2 * CAPACITY, 2), (CAPACITY, 30)], key=lambda r: r[1])
    grant = min(8, CAPACITY)
    got, code = await _run(dut, rows, gap=3, granted_keys=grant)
    expected, expected_code = sessionize_scan(rows, gap=3, capacity=grant)
    assert code == expected_code
    assert got == expected


@cocotb.test()
async def session_gap_is_patient(dut):
    """The binding LID requirement: random relay-station delays on every
    channel change NOTHING about the emitted sequence. State is fresh per
    batch, so the same rows replay identically by construction."""
    await _start(dut)
    rng = random.Random(7)
    times, rows = 0, []
    for _ in range(48):
        times += rng.randint(0, 6)
        rows.append((rng.randint(-4, 4), times))
    grant = min(16, CAPACITY)
    expected, expected_code = sessionize_scan(rows, gap=4, capacity=grant)
    for seed in (1, 2, 3):
        got, code = await _run(dut, rows, gap=4, granted_keys=grant, delay_seed=seed)
        assert code == expected_code, seed
        assert got == expected, seed


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.cfg_gap.value = 0
    dut.cfg_granted_keys.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, *, gap, granted_keys, delay_seed=None):
    dut.cfg_gap.value = gap & 0xFFFFFFFF
    dut.cfg_granted_keys.value = granted_keys & 0xFFFFFFFF
    gap_fn = random.Random(delay_seed).randint if delay_seed is not None else None
    stall = random.Random((delay_seed or 0) + 100).randint if delay_seed is not None else None
    send = cocotb.start_soon(_send_rows(dut, rows, gap=gap_fn))
    words = await _collect_output(dut, stall=stall)
    await send
    error, code = await _accept_status(dut)
    assert len(words) % 2 == 0, "sessions are two words"
    sessions = []
    for offset in range(0, len(words), 2):
        w0, w1 = words[offset], words[offset + 1]
        # key/start/last are int32 WIRES (signed); count is a CARDINALITY —
        # unsigned on the wire, matching the golden's unbounded int for every
        # batch below 2^32 rows (16+ GiB of rows, beyond any staging region)
        sessions.append((_signed32(w0 & 0xFFFFFFFF), _signed32(w0 >> 32), _signed32(w1 & 0xFFFFFFFF), w1 >> 32))
    return sessions, SessionErrorCode(code) if error else SessionErrorCode.OK


async def _send_rows(dut, rows, *, gap=None) -> None:
    for index, (key, time) in enumerate(rows):
        if gap is not None:
            for _ in range(gap(0, 4)):
                dut.input_valid.value = 0
                await RisingEdge(dut.clk)
        dut.input_data.value = ((time & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
        dut.input_last.value = int(index == len(rows) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _collect_output(dut, *, stall=None):
    words = []
    budget = 4 * CAPACITY * (CAPACITY + 8) + 65536  # the O(C x live) drain
    for _ in range(budget):
        ready = 1
        if stall is not None and stall(0, 4) == 0:
            ready = 0
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
    raise AssertionError("timed out collecting session output")


async def _accept_status(dut):
    dut.status_ready.value = 1
    for _ in range(4 * CAPACITY + 256):
        if int(dut.status_valid.value) == 1:
            error = int(dut.status_error.value) == 1
            code = int(dut.status_error_code.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return (error, code)
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for close-out")


def _signed32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - (1 << 32) if value >= (1 << 31) else value

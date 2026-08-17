from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import KeyedStateErrorCode, KeyedStateOp, keyed_state_scan

CAPACITY = int(os.environ.get("DAU_KEYED_STATE_CAPACITY", "1024"))

# (key, value): repeats, interleaves, a negative key (a full int32 wire)
_ROWS = [(1, 5), (2, 3), (1, 4), (-7, 10), (1, -2), (2, 1), (-7, 1)]


@cocotb.test()
async def keyed_state_add_matches_golden(dut):
    await _start(dut)
    got, code = await _run(dut, _ROWS, op=KeyedStateOp.ADD, granted_keys=8)
    expected, expected_code = keyed_state_scan(_ROWS, op=KeyedStateOp.ADD, capacity=8)
    assert code == expected_code
    assert got == expected


@cocotb.test()
async def keyed_state_every_op_matches_golden(dut):
    await _start(dut)
    for op in (KeyedStateOp.SET, KeyedStateOp.MAX, KeyedStateOp.MIN):
        got, code = await _run(dut, _ROWS, op=op, granted_keys=8, initial=100)
        expected, expected_code = keyed_state_scan(_ROWS, op=op, capacity=8, initial=100)
        assert code == expected_code, op
        assert got == expected, op


@cocotb.test()
async def keyed_state_batches_are_independent(dut):
    # fresh state per batch, matching the golden: batch 2 must not see batch
    # 1's keys (the CLEAR sweep under test)
    await _start(dut)
    for _ in range(2):
        got, code = await _run(dut, [(1, 5), (1, 5)], op=KeyedStateOp.ADD, granted_keys=4)
        assert code is KeyedStateErrorCode.OK
        assert got == [(1, 5), (1, 10)]  # NOT (1, 15) on the second batch


@cocotb.test()
async def keyed_state_grant_exceeded_preserves_prefix(dut):
    await _start(dut)
    rows = [(1, 5), (2, 3), (3, 1)]  # third distinct key past a grant of 2
    expected, expected_code = keyed_state_scan(rows, op=KeyedStateOp.ADD, capacity=2)
    got, code = await _run(dut, rows, op=KeyedStateOp.ADD, granted_keys=2)
    assert expected_code is KeyedStateErrorCode.KEY_CAPACITY_EXCEEDED
    assert code == expected_code
    assert got == expected  # [(1, 5), (2, 3)]


@cocotb.test()
async def keyed_state_bad_grant_is_refused(dut):
    await _start(dut)
    for grant in (0, CAPACITY + 1):
        got, code = await _run(dut, [(1, 1), (2, 2)], op=KeyedStateOp.ADD, granted_keys=grant)
        assert code is KeyedStateErrorCode.BAD_CAPACITY
        assert got == []


@cocotb.test()
async def keyed_state_wraps_in_the_int32_domain(dut):
    await _start(dut)
    rows = [(1, 2**31 - 1), (1, 1)]
    got, code = await _run(dut, rows, op=KeyedStateOp.ADD, granted_keys=4)
    expected, expected_code = keyed_state_scan(rows, op=KeyedStateOp.ADD, capacity=4)
    assert code == expected_code
    assert got == expected  # [(1, 2**31 - 1), (1, -(2**31))]


@cocotb.test()
async def keyed_state_colliding_keys_probe_correctly(dut):
    # keys congruent mod CAPACITY exercise the linear probe explicitly
    await _start(dut)
    rows = [(0, 1), (CAPACITY, 2), (2 * CAPACITY, 3), (CAPACITY, 4)]
    grant = min(8, CAPACITY)
    got, code = await _run(dut, rows, op=KeyedStateOp.ADD, granted_keys=grant)
    expected, expected_code = keyed_state_scan(rows, op=KeyedStateOp.ADD, capacity=grant)
    assert code == expected_code
    assert got == expected


@cocotb.test()
async def keyed_state_is_patient(dut):
    """The binding LID requirement (STORAGE_AND_NOC.md backpressure rule 1):
    random relay-station delays on every channel change NOTHING about the
    emitted sequence. No write-prefix is needed here — state is fresh per
    batch, so the same rows replay identically by construction."""
    await _start(dut)
    rng = random.Random(7)
    rows = [(rng.randint(-8, 8), rng.randint(-100, 100)) for _ in range(64)]
    grant = min(32, CAPACITY)  # the grant can never exceed the table
    expected, expected_code = keyed_state_scan(rows, op=KeyedStateOp.ADD, capacity=grant)
    for seed in (1, 2, 3):
        got, code = await _run(dut, rows, op=KeyedStateOp.ADD, granted_keys=grant, delay_seed=seed)
        assert code == expected_code, seed
        assert got == expected, seed


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.cfg_op.value = 0
    dut.cfg_initial.value = 0
    dut.cfg_granted_keys.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, *, op, granted_keys, initial=0, delay_seed=None):
    dut.cfg_op.value = int(op)
    dut.cfg_initial.value = initial & 0xFFFFFFFF
    dut.cfg_granted_keys.value = granted_keys & 0xFFFFFFFF
    gap = random.Random(delay_seed).randint if delay_seed is not None else None
    stall = random.Random((delay_seed or 0) + 100).randint if delay_seed is not None else None
    send = cocotb.start_soon(_send_rows(dut, rows, gap=gap))
    words = await _collect_output(dut, stall=stall)
    await send
    error, code = await _accept_status(dut)
    emitted = [(_signed32(w & 0xFFFFFFFF), _signed32(w >> 32)) for w in words]
    return emitted, KeyedStateErrorCode(code) if error else KeyedStateErrorCode.OK


async def _send_rows(dut, rows, *, gap=None) -> None:
    for index, (key, value) in enumerate(rows):
        if gap is not None:
            for _ in range(gap(0, 4)):
                dut.input_valid.value = 0
                await RisingEdge(dut.clk)
        dut.input_data.value = ((value & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
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
    for _ in range(65536):
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
    raise AssertionError("timed out collecting keyed-state output")


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

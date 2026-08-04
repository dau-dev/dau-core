from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import ScratchpadErrorCode, scratchpad_scan

CAPACITY = int(os.environ.get("DAU_SCRATCHPAD_CAPACITY", "1024"))

# (write_enable, addr, value): writes then readback, including a rewrite
_ROWS = [
    (1, 0, 7),
    (0, 0, 999),  # read ignores the value field
    (1, 3, -5),
    (0, 3, 0),
    (1, 0, 11),  # rewrite
    (0, 0, 0),
    (0, 2, 0),  # unwritten word reads 0 (valid because this test runs FIRST on a fresh sim)
]


@cocotb.test()
async def scratchpad_matches_golden(dut):
    await _start(dut)
    got, code = await _run(dut, _ROWS, granted_words=8)
    expected, expected_code = scratchpad_scan(_ROWS, granted_words=8)
    assert code == expected_code
    assert got == expected


@cocotb.test()
async def scratchpad_overflow_closes_out_and_preserves_prefix(dut):
    await _start(dut)
    rows = [(1, 0, 5), (1, 8, 1), (1, 1, 9)]  # addr 8 >= grant 8
    expected, expected_code = scratchpad_scan(rows, granted_words=8)
    got, code = await _run(dut, rows, granted_words=8)
    assert expected_code is ScratchpadErrorCode.STORAGE_OVERFLOW
    assert code == expected_code
    assert got == expected  # the emitted prefix survives the error


@cocotb.test()
async def scratchpad_negative_address_is_never_in_grant(dut):
    await _start(dut)
    rows = [(1, -1, 5)]
    got, code = await _run(dut, rows, granted_words=8)
    assert code is ScratchpadErrorCode.STORAGE_OVERFLOW
    assert got == []


@cocotb.test()
async def scratchpad_bad_grant_is_refused(dut):
    await _start(dut)
    for grant in (0, CAPACITY + 1):
        got, code = await _run(dut, [(1, 0, 1), (0, 0, 0)], granted_words=grant)
        assert code is ScratchpadErrorCode.BAD_GRANT
        assert got == []


@cocotb.test()
async def scratchpad_is_patient(dut):
    """The binding LID requirement (STORAGE_AND_NOC.md backpressure rule 1):
    random relay-station delays on every channel change NOTHING about the
    emitted sequence."""
    await _start(dut)
    rng = random.Random(7)
    # the arena PERSISTS across batches (owned storage; grants outlive jobs),
    # while the golden models one batch on a fresh arena. Golden-equality
    # therefore requires every read to follow a write within the batch: the
    # write prefix makes the sequence independent of arena history, so the
    # same rows replay bit-identically under every delay seed.
    rows = [(1, addr, rng.randint(-100, 100)) for addr in range(16)]
    rows += [(rng.randint(0, 1), rng.randint(0, 15), rng.randint(-100, 100)) for _ in range(64)]
    expected, expected_code = scratchpad_scan(rows, granted_words=16)
    for seed in (1, 2, 3):
        got, code = await _run(dut, rows, granted_words=16, delay_seed=seed)
        assert code == expected_code, seed
        assert got == expected, seed


@cocotb.test()
async def scratchpad_torn_row_reports_stream_error(dut):
    await _start(dut)
    dut.cfg_granted_words.value = 8
    dut.output_ready.value = 1
    dut.input_data.value = 5
    dut.input_last.value = 1  # input_last on w0: a torn one-word row
    dut.input_valid.value = 1
    while True:
        await ReadOnly()
        fired = int(dut.input_ready.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            break
    dut.input_valid.value = 0
    dut.input_last.value = 0
    code = await _accept_status(dut)
    assert code == (True, 3)  # ERR_STREAM, an HDL-only tearing error


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.cfg_granted_words.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, *, granted_words, delay_seed=None):
    dut.cfg_granted_words.value = granted_words & 0xFFFFFFFF
    gap = random.Random(delay_seed).randint if delay_seed is not None else None
    stall = random.Random((delay_seed or 0) + 100).randint if delay_seed is not None else None
    send = cocotb.start_soon(_send_rows(dut, rows, gap=gap))
    words = await _collect_output(dut, stall=stall)
    await send
    error, code = await _accept_status(dut)
    emitted = [(_signed32(w & 0xFFFFFFFF), _signed32(w >> 32)) for w in words]
    return emitted, ScratchpadErrorCode(code) if error else ScratchpadErrorCode.OK


async def _send_rows(dut, rows, *, gap=None) -> None:
    words = []
    for write_enable, addr, value in rows:
        words.append(((value & 0xFFFFFFFF) << 32) | (addr & 0xFFFFFFFF))
        words.append(write_enable & 1)
    for index, word in enumerate(words):
        if gap is not None:
            for _ in range(gap(0, 4)):
                dut.input_valid.value = 0
                await RisingEdge(dut.clk)
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


async def _collect_output(dut, *, stall=None):
    words = []
    for _ in range(16384):
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
    raise AssertionError("timed out collecting scratchpad output")


async def _accept_status(dut):
    dut.status_ready.value = 1
    for _ in range(256):
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

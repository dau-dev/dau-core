from __future__ import annotations

import os

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import time_bucket_key_rows
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

BUCKET_BITS = int(os.environ.get("DAU_BUCKET_BITS", "16"))
BUCKET_SHIFT = 3  # window width 8

# (partition, payload_hi, time, payload2, payload3)
_ROWS = [
    (570, 11, 100, 5, 0),  # bucket 100>>3 = 12
    (570, 12, 103, 3, 0),  # bucket 12 (same bar)
    (570, 13, 112, 2, 0),  # bucket 14 (new bar)
    (571, 50, 100, 7, 0),  # partition change
    (571, 55, 130, 4, 0),  # bucket 16
    (572, -3, 5, 1, 0),  # bucket 0
]


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    rows = [(index % 4, index, index << BUCKET_SHIFT, index + 1, index + 2) for index in range(256)]
    await assert_sustained_ingest(
        dut,
        "int32-time-bucket-key",
        send=lambda: _send_rows(dut, rows),
        input_beats=3 * len(rows),
        beats_per_row=3,
    )


@cocotb.test()
async def bucket_key_matches_golden(dut):
    await _start(dut)
    rows = await _run(dut, _ROWS)
    assert rows == time_bucket_key_rows(_ROWS, bucket_shift=BUCKET_SHIFT, bucket_bits=BUCKET_BITS)


@cocotb.test()
async def bucket_key_backpressure(dut):
    await _start(dut)
    rows = await _run(dut, _ROWS, ready_pattern=(1, 0, 0, 1))
    assert rows == time_bucket_key_rows(_ROWS, bucket_shift=BUCKET_SHIFT, bucket_bits=BUCKET_BITS)


@cocotb.test()
async def bucket_key_partition_and_bucket_pack_disjointly(dut):
    await _start(dut)
    rows = await _run(dut, _ROWS)
    # partition recovers as key >> BUCKET_BITS; bucket as key & mask
    for (partition, _hi, time, _p2, _p3), (key, _khi, _kp2, _kp3) in zip(_ROWS, rows, strict=True):
        assert (key >> BUCKET_BITS) == (partition & ((1 << (32 - BUCKET_BITS)) - 1))
        assert (key & ((1 << BUCKET_BITS) - 1)) == ((time >> BUCKET_SHIFT) & ((1 << BUCKET_BITS) - 1))


@cocotb.test()
async def bucket_key_single_row(dut):
    await _start(dut)
    assert await _run(dut, [(9, 42, 64, 3, 7)]) == time_bucket_key_rows([(9, 42, 64, 3, 7)], bucket_shift=BUCKET_SHIFT, bucket_bits=BUCKET_BITS)


@cocotb.test()
async def bucket_key_truncated_row_reports_stream_error(dut):
    await _start(dut)
    dut.output_ready.value = 1
    dut.input_data.value = 570  # w0 with last: a torn row
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
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == 2
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for error status")


@cocotb.test()
async def bucket_key_partition_overflow_is_range_error(dut):
    await _start(dut)
    dut.output_ready.value = 1
    # a partition that does not fit PART_BITS = 32 - BUCKET_BITS would alias into
    # another group; the tile refuses with ERR_RANGE rather than truncating
    over = 1 << (32 - BUCKET_BITS)  # first partition id that overflows PART_BITS
    await _send_rows(dut, [(over, 1, 64, 2, 3)])
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == 3  # ERR_RANGE
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for range error")


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    dut.cfg_bucket_shift.value = BUCKET_SHIFT
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, *, ready_pattern=(1,)):
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_output(dut, ready_pattern)
    await send
    assert len(words) % 2 == 0, "output rows are 2 words"
    return [_decode(words[offset : offset + 2]) for offset in range(0, len(words), 2)]


def _decode(words):
    key = words[0] & 0xFFFFFFFF
    payload_hi = _s32(words[0] >> 32)
    payload2 = _s32(words[1] & 0xFFFFFFFF)
    payload3 = _s32(words[1] >> 32)
    return (key, payload_hi, payload2, payload3)


async def _send_rows(dut, rows) -> None:
    words = []
    for partition, payload_hi, time, payload2, payload3 in rows:
        words.append(((payload_hi & 0xFFFFFFFF) << 32) | (partition & 0xFFFFFFFF))
        words.append(time & ((1 << 64) - 1))
        words.append(((payload3 & 0xFFFFFFFF) << 32) | (payload2 & 0xFFFFFFFF))
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
    raise AssertionError("timed out collecting bucket output")


def _s32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value

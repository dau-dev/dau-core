"""IN_WIDTH 256/512 (R = 2/4 quad rows per beat): the wide beat
deserializes onto the internal single-row feed (one row per cycle,
lowest index first), drops the reserved all-ones PAD ROW (ragged-tail
padding), and stays golden-equal to ``mask_dispatch_rows`` under
same-lane collisions and backpressure. Same routing datapath as 128; the
width buys DDR-side burst width, not (yet) rows/cycle. The width-agnostic
collect harness is shared with the 64-bit bench."""

from __future__ import annotations

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import mask_dispatch_rows
from dau_core.tests.cocotb_benches.dau_int32_key_mask_dispatcher_tb import (
    _drive_and_collect,
    _num_parts,
    _random_rows,
)
from dau_core.tests.cocotb_benches.dau_int32_key_mask_dispatcher_wide_tb import (
    _row_beat,
    _start,
)

_PAD_ROW_BITS = (1 << 128) - 1


def _rows_per_beat(dut) -> int:
    return len(dut.input_data) // 128


async def _send_multi_beats(dut, rows) -> None:
    r = _rows_per_beat(dut)
    beats = []
    for start in range(0, len(rows), r):
        chunk = rows[start : start + r]
        beat = 0
        for i in range(r):
            bits = _row_beat(chunk[i]) if i < len(chunk) else _PAD_ROW_BITS
            beat |= bits << (128 * i)
        beats.append(beat)
    for index, beat in enumerate(beats):
        dut.input_data.value = beat
        dut.input_last.value = int(index == len(beats) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _run(dut, rows, *, ready_pattern=(1,)):
    golden = mask_dispatch_rows(rows, num_partitions=_num_parts(dut))
    send = cocotb.start_soon(_send_multi_beats(dut, rows))
    parts = await _drive_and_collect(dut, golden, ready_pattern)
    await send
    return parts


@cocotb.test()
async def multi_row_dispatch_matches_the_golden(dut):
    await _start(dut)
    rng = random.Random(20260730)
    rows = _random_rows(rng)
    parts = await _run(dut, rows)
    assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut))


@cocotb.test()
async def same_lane_collisions_serialize_in_arrival_order(dut):
    # every row targets ONE lane: the serializer presents rows in index
    # order, so the lane sees arrival order — golden-equal
    await _start(dut)
    n = _num_parts(dut)
    rows = [(n * i, i + 1, 2 * i, 3 * i) for i in range(24)]  # key % n == 0
    parts = await _run(dut, rows)
    assert parts == mask_dispatch_rows(rows, num_partitions=n)


@cocotb.test()
async def ragged_tail_pad_rows_are_dropped(dut):
    # a row count that is not a multiple of R: the final beat carries the
    # all-ones pad, which must not reach any lane
    await _start(dut)
    r = _rows_per_beat(dut)
    rows = [(i, i + 10, i + 20, i + 30) for i in range(4 * r + 1)]
    parts = await _run(dut, rows)
    assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut))


@cocotb.test()
async def all_ones_key_still_routes_when_row_is_not_pad(dut):
    # only the FULL all-ones 128-bit row is the pad: a row whose key is
    # all-ones (-1 on the wire = 0xFFFFFFFF) but whose data fields differ is
    # NOT the pad and must route normally (-1 & mask selects its lane)
    await _start(dut)
    rows = [(-1, 7, 8, 9), (1, 2, 3, 4)]
    parts = await _run(dut, rows)
    assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut))


@cocotb.test()
async def backpressured_lanes_stay_golden_equal(dut):
    await _start(dut)
    rng = random.Random(20260731)
    rows = _random_rows(rng)
    parts = await _run(dut, rows, ready_pattern=(1, 0, 0))
    assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut))


@cocotb.test()
async def a_trailing_all_pad_beat_still_closes_the_batch(dut):
    # defensive framing: a real row count that fills exactly R rows, then an
    # extra all-pad beat marked last — every real row routes and the batch
    # closes (no wedge). Built by hand-driving beats since the packer never
    # emits an all-pad beat.
    await _start(dut)
    r = _rows_per_beat(dut)
    rows = [(i, i + 10, i + 20, i + 30) for i in range(r)]
    n = _num_parts(dut)
    golden = mask_dispatch_rows(rows, num_partitions=n)

    async def _send():
        # beat 0: the R real rows, NOT last
        beat = 0
        for i in range(r):
            beat |= _row_beat(rows[i]) << (128 * i)
        dut.input_data.value = beat
        dut.input_last.value = 0
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
        # beat 1: all pad, last
        dut.input_data.value = (1 << (128 * r)) - 1
        dut.input_last.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
        dut.input_valid.value = 0
        dut.input_last.value = 0

    send = cocotb.start_soon(_send())
    parts = await _drive_and_collect(dut, golden, (1,))
    await send
    assert parts == golden

"""Positional wide-beat distribution onto narrow stateful lanes."""

from __future__ import annotations

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import stride_distribute_rows
from dau_core.tests.cocotb_benches.dau_key_mask_dispatcher_tb import (
    _drive_and_collect,
    _num_parts,
)
from dau_core.tests.cocotb_benches.dau_key_mask_dispatcher_wide_tb import (
    _row_beat,
    _start,
)

_PAD_ROW_BITS = (1 << 128) - 1


def _rows_per_beat(dut) -> int:
    return len(dut.input_data) // 128


def _pack_beat(rows, rows_per_beat: int) -> int:
    beat = 0
    for slot in range(rows_per_beat):
        bits = _row_beat(rows[slot]) if slot < len(rows) else _PAD_ROW_BITS
        beat |= bits << (128 * slot)
    return beat


async def _send_rows(dut, rows, *, trailing_pad_beat: bool = False) -> None:
    r = _rows_per_beat(dut)
    beats = [_pack_beat(rows[start : start + r], r) for start in range(0, len(rows), r)]
    if trailing_pad_beat:
        beats.append((1 << (128 * r)) - 1)

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


async def _run(dut, rows, *, ready_pattern=(1,), trailing_pad_beat: bool = False):
    golden = stride_distribute_rows(rows, rows_per_beat=_rows_per_beat(dut), num_partitions=_num_parts(dut))
    send = cocotb.start_soon(_send_rows(dut, rows, trailing_pad_beat=trailing_pad_beat))
    parts = await _drive_and_collect(dut, golden, ready_pattern)
    await send
    return tuple(parts)


@cocotb.test()
async def random_rows_match_static_slot_golden(dut):
    await _start(dut)
    rng = random.Random(0x57_21_DE)
    r = _rows_per_beat(dut)
    rows = [(rng.randint(-100, 100), rng.randint(-1000, 1000), rng.randint(-1000, 1000), rng.randint(-1000, 1000)) for _ in range(8 * r)]
    parts = await _run(dut, rows)
    assert parts == stride_distribute_rows(rows, rows_per_beat=r, num_partitions=_num_parts(dut))


@cocotb.test()
async def ragged_tail_pads_are_dropped_under_per_lane_backpressure(dut):
    await _start(dut)
    rng = random.Random(0xBACC)
    r = _rows_per_beat(dut)
    rows = [(i, rng.randint(-100, 100), i * 3, -i) for i in range(5 * r + 1)]
    parts = await _run(dut, rows, ready_pattern=(1, 0, 0, 1, 0))
    assert parts == stride_distribute_rows(rows, rows_per_beat=r, num_partitions=_num_parts(dut))


@cocotb.test()
async def trailing_all_pad_last_beat_flushes_every_lane(dut):
    await _start(dut)
    r = _rows_per_beat(dut)
    rows = [(i, i + 10, i + 20, i + 30) for i in range(3 * r)]
    parts = await _run(dut, rows, ready_pattern=(1, 0, 1), trailing_pad_beat=True)
    assert parts == stride_distribute_rows(rows, rows_per_beat=r, num_partitions=_num_parts(dut))


@cocotb.test()
async def a_batch_smaller_than_the_lane_count_still_closes_every_lane(dut):
    """A real shell gives every lane a record writer whose done waits on
    the batch closing, so lanes that receive ZERO rows must still see the
    batch end — the single-row batch is the extreme case (one lane gets
    the row, all the others get nothing)."""
    await _start(dut)
    rows = [(7, 11, 13, 17)]
    parts = await _run(dut, rows)
    golden = stride_distribute_rows(rows, rows_per_beat=_rows_per_beat(dut), num_partitions=_num_parts(dut))
    assert parts == golden
    assert sum(len(part) for part in parts) == 1
    assert sum(1 for part in parts if part) == 1


@cocotb.test()
async def load_is_even_by_construction_no_lane_runs_hot(dut):
    """The stride distributor's reason to exist: routing by SLOT POSITION
    rather than by key means no key distribution can make one lane hot.
    The key-mask dispatcher loses ~20% to exactly that (same-lane key
    runs); here an adversarial all-equal-key stream still spreads evenly."""
    await _start(dut)
    r = _rows_per_beat(dut)
    lanes = _num_parts(dut)
    rows = [(42, i, i, i) for i in range(8 * lanes)]  # every key identical
    parts = await _run(dut, rows)
    assert parts == stride_distribute_rows(rows, rows_per_beat=r, num_partitions=lanes)
    counts = [len(part) for part in parts]
    assert max(counts) - min(counts) <= 1, f"uneven load {counts} under an adversarial key stream"

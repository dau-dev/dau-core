"""RECORD_WORDS != 2: the dispatcher routes N-word records whole.

The quad-row form carries two 64-bit words; a fused chain whose head reads
wider records (the as-of join's three-word {tag/key, time, payload}) must be
replicable across lanes WITHOUT reshaping the stream, so the router forwards
whole records keyed on word 0's low half. Golden-equal to
``mask_dispatch_records``; ordering within a lane is what makes a
sequential per-key tile correct behind the router."""

from __future__ import annotations

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import mask_dispatch_records
from dau_core.tests.cocotb_benches.dau_int32_key_mask_dispatcher_tb import _num_parts, _start

_MASK64 = (1 << 64) - 1


# the runner elaborates this bench at RECORD_WORDS=3 (the as-of join's record)
_RECORD_WORDS = [3]


async def _send(dut, records) -> None:
    flat = [w for record in records for w in record]
    for index, word in enumerate(flat):
        dut.input_data.value = word & _MASK64
        dut.input_last.value = int(index == len(flat) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _collect(dut, expected, ready_pattern=(1,)):
    lanes = _num_parts(dut)
    words = _RECORD_WORDS[0]
    got: list[list[int]] = [[] for _ in range(lanes)]
    done = [False] * lanes
    for tick in range(400_000):
        ready = ready_pattern[tick % len(ready_pattern)]
        dut.output_ready.value = int(("1" * lanes) if ready else "0" * lanes, 2)
        await ReadOnly()
        valid = int(dut.output_valid.value)
        last = int(dut.output_last.value)
        data = int(dut.output_data.value)
        if ready:
            for lane in range(lanes):
                if valid >> lane & 1:
                    got[lane].append((data >> (64 * lane)) & _MASK64)
                    if last >> lane & 1:
                        done[lane] = True
        await RisingEdge(dut.clk)
        if all(done):
            break
    dut.output_ready.value = 0
    packed = [[tuple(lane_words[i : i + words]) for i in range(0, len(lane_words), words)] for lane_words in got]
    assert packed == expected, f"routing mismatch: {[len(p) for p in packed]} vs {[len(e) for e in expected]}"
    return packed


async def _run(dut, records, ready_pattern=(1,)):
    expected = mask_dispatch_records(records, num_partitions=_num_parts(dut))
    send = cocotb.start_soon(_send(dut, records))
    packed = await _collect(dut, expected, ready_pattern)
    await send
    return packed


def _record(symbol: int, rng: random.Random) -> tuple[int, ...]:
    # {tag,key}, time, {payload_b,payload_a} — the as-of join's wire record
    return (
        (rng.randint(0, 1) << 32) | (symbol & 0xFFFF_FFFF),
        rng.randint(0, 1 << 40),
        (rng.randint(0, 1 << 30) << 32) | rng.randint(0, 1 << 30),
    )


@cocotb.test()
async def three_word_records_route_whole_and_in_order(dut):
    await _start(dut)
    rng = random.Random(20260801)
    lanes = _num_parts(dut)
    records = [_record(rng.randrange(4 * lanes), rng) for _ in range(12 * lanes)]
    await _run(dut, records)


@cocotb.test()
async def per_key_order_is_preserved_within_a_lane(dut):
    """A sequential per-key tile (an as-of join holding one quote per
    symbol) is only correct behind this router if a key's records reach
    its lane in arrival order."""
    await _start(dut)
    lanes = _num_parts(dut)
    symbol = lanes + 1  # a single key, so every record lands in one lane
    records = [(symbol, t, t * 3) for t in range(40)]
    packed = await _run(dut, records)
    lane = symbol & (lanes - 1)
    assert [r[1] for r in packed[lane]] == list(range(40)), "arrival order lost"
    assert sum(len(p) for p in packed) == 40


@cocotb.test()
async def a_lane_receiving_nothing_still_closes(dut):
    await _start(dut)
    rng = random.Random(11)
    lanes = _num_parts(dut)
    records = [_record(0, rng) for _ in range(5)]  # every record to lane 0
    packed = await _run(dut, records)
    assert len(packed[0]) == 5
    assert all(not packed[lane] for lane in range(1, lanes))


@cocotb.test()
async def backpressured_lanes_stay_golden_equal(dut):
    await _start(dut)
    rng = random.Random(20260802)
    lanes = _num_parts(dut)
    records = [_record(rng.randrange(3 * lanes), rng) for _ in range(10 * lanes)]
    await _run(dut, records, ready_pattern=(1, 0, 0))

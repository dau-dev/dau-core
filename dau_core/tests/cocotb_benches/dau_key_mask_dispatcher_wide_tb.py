from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import mask_dispatch_rows

# IN_WIDTH == 128 (the rows/cycle axis): the same dispatcher accepting ONE
# whole quad row per input beat ({w1, w0}). Runner passes
# parameters={"IN_WIDTH": 128}; the width-agnostic multi-lane collect harness
# is shared with the 64-bit bench.
from dau_core.tests.cocotb_benches.dau_key_mask_dispatcher_tb import (
    _MASK32,
    _MASK64,
    _ROWS,
    _drive_and_collect,
    _num_parts,
    _random_rows,
    _unpack_rows,
)
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest


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


def _row_beat(row) -> int:
    key, value, aux0, aux1 = row
    w0 = ((value & _MASK32) << 32) | (key & _MASK32)
    w1 = ((aux1 & _MASK32) << 32) | (aux0 & _MASK32)
    return (w1 << 64) | w0


async def _send_row_beats(dut, rows) -> None:
    for index, row in enumerate(rows):
        dut.input_data.value = _row_beat(row)
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


async def _run(dut, rows, *, ready_pattern=(1,)):
    golden = mask_dispatch_rows(rows, num_partitions=_num_parts(dut))
    send = cocotb.start_soon(_send_row_beats(dut, rows))
    parts = await _drive_and_collect(dut, golden, ready_pattern)
    await send
    return parts


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    num_parts = _num_parts(dut)
    rows = [(index % num_parts, index, index + 1, index + 2) for index in range(256)]
    await assert_sustained_ingest(
        dut,
        "key-mask-dispatcher",
        send=lambda: _send_row_beats(dut, rows),
        input_beats=len(rows),
        params={"IN_WIDTH": 128},
    )


@cocotb.test()
async def wide_dispatch_routes_one_row_per_beat(dut):
    await _start(dut)
    parts = await _run(dut, _ROWS)
    assert parts == mask_dispatch_rows(_ROWS, num_partitions=_num_parts(dut))


@cocotb.test()
async def wide_front_sustains_one_row_per_cycle_on_rotating_keys(dut):
    # THE AXIS PROPERTY: with keys rotating across the lanes (each lane sees
    # 1/N of the rows) and every sink ready, the shared front accepts N rows in
    # ~N cycles — each lane's two-beat drain overlaps the other lanes' pushes,
    # so no lane's skid ever back-pressures the front. Outputs are collected in
    # the SAME loop (lanes drain concurrently with the accepts) and must match
    # the golden routing.
    await _start(dut)
    num_parts = _num_parts(dut)
    rows = [(i, i * 3, i & 0x7F, 7) for i in range(64)]  # key = i rotates lanes
    golden = mask_dispatch_rows(rows, num_partitions=num_parts)
    words: list[list[int]] = [[] for _ in range(num_parts)]
    expect_output = [len(golden[p]) != 0 for p in range(num_parts)]
    done = [not expected for expected in expect_output]  # zero-row lanes: status
    send = cocotb.start_soon(_send_row_beats(dut, rows))
    dut.output_ready.value = (1 << num_parts) - 1
    dut.status_ready.value = (1 << num_parts) - 1
    accepts = 0
    first_accept = None
    last_accept = None
    for tick in range(1, 20001):
        await ReadOnly()
        if int(dut.input_valid.value) == 1 and int(dut.input_ready.value) == 1:
            accepts += 1
            last_accept = tick
            if first_accept is None:
                first_accept = tick
        ov = int(dut.output_valid.value)
        od = int(dut.output_data.value)
        ol = int(dut.output_last.value)
        sv = int(dut.status_valid.value)
        se = int(dut.status_error.value)
        for p in range(num_parts):
            if (ov >> p) & 1:
                words[p].append((od >> (p * 64)) & _MASK64)
                if (ol >> p) & 1:
                    done[p] = True
            if not expect_output[p] and (sv >> p) & 1:
                assert (se >> p) & 1 == 0, f"empty lane {p} raised an error status"
                done[p] = True
        await RisingEdge(dut.clk)
        if accepts == len(rows) and all(done):
            break
    await send
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    assert accepts == len(rows), f"only {accepts}/{len(rows)} rows accepted"
    assert all(done), f"lanes never completed: {done}"
    assert [_unpack_rows(words[p]) for p in range(num_parts)] == golden
    assert first_accept is not None and last_accept is not None
    window = last_accept - first_accept + 1
    assert window <= len(rows) + 4, f"wide front took {window} cycles to accept {len(rows)} rotating-key rows (not 1 row/cycle)"


@cocotb.test()
async def wide_same_lane_run_throttles_but_stays_correct(dut):
    # every row to one lane: the front throttles on that lane's two-beat drain
    # (single-row skid) — correctness and ordering must hold under the stall
    await _start(dut)
    num_parts = _num_parts(dut)
    rows = [(num_parts * i + 1, i, i, 0) for i in range(1, 12)]  # all lane 1 (key & (N-1) == 1)
    parts = await _run(dut, rows)
    assert parts == mask_dispatch_rows(rows, num_partitions=num_parts)


@cocotb.test()
async def wide_backpressure_holds_rows_stable(dut):
    await _start(dut)
    parts = await _run(dut, _ROWS, ready_pattern=(1, 0, 0, 1, 0))
    assert parts == mask_dispatch_rows(_ROWS, num_partitions=_num_parts(dut))


@cocotb.test()
async def wide_single_row_batch_lands_with_last(dut):
    # one row: its lane emits it tagged last; every other lane closes out
    # zero-row success
    await _start(dut)
    rows = [(2, 42, 7, 9)]
    parts = await _run(dut, rows)
    assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut))


@cocotb.test()
async def wide_repeated_batches_back_to_back(dut):
    await _start(dut)
    for _ in range(3):
        parts = await _run(dut, _ROWS)
        assert parts == mask_dispatch_rows(_ROWS, num_partitions=_num_parts(dut))


@cocotb.test()
async def wide_constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0xA11CE)
    for trial in range(25):
        rows = _random_rows(rng)
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):
            pattern = pattern + (1,)
        parts = await _run(dut, rows, ready_pattern=pattern)
        assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut)), f"trial {trial}: rows={rows}"

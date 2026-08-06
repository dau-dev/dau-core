from __future__ import annotations

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import DispatchMode, mask_dispatch_rows

# MODE == DispatchMode.EXACT_BRANCH (the q19 disjunct-branch mode): dest = key - 1 for keys
# in [1, NUM_PARTITIONS], every other key dropped at the front for zero lane
# cycles. Runner passes parameters={"MODE": 1, "NUM_PARTITIONS": 3}
# (no power-of-two) at either IN_WIDTH; the width is detected from the dut so
# the same module covers the wide and narrow fronts.
from dau_core.tests.cocotb_benches.dau_int32_key_mask_dispatcher_tb import (
    _drive_and_collect,
    _num_parts,
    _send_rows,
)
from dau_core.tests.cocotb_benches.dau_int32_key_mask_dispatcher_wide_tb import (
    _send_row_beats,
    _start,
)

# keys: valid branches (1..3), branch 0 (dominant drop class), out-of-range
# positives, and negatives (unsigned image out of range -> dropped)
_EXACT_ROWS = [
    (1, 100, 10, 500_000),
    (0, 105, 3, 0),
    (2, 95, 2, 250_000),
    (0, 102, 1, 999_999),
    (3, 102, 7, 0),
    (7, 99, 4, 100_000),
    (-1, 88, 9, 1),
    (2, 77, 5, 42),
    (0, 66, 6, 7),
]


def _wide(dut) -> bool:
    return len(dut.input_data) == 128


async def _send(dut, rows) -> None:
    if _wide(dut):
        await _send_row_beats(dut, rows)
    else:
        await _send_rows(dut, rows)


async def _run(dut, rows, *, ready_pattern=(1,)):
    golden = mask_dispatch_rows(rows, num_partitions=_num_parts(dut), mode=DispatchMode.EXACT_BRANCH)
    send = cocotb.start_soon(_send(dut, rows))
    parts = await _drive_and_collect(dut, golden, ready_pattern)
    await send
    return parts


@cocotb.test()
async def exact_branch_routes_and_drops(dut):
    await _start(dut)
    parts = await _run(dut, _EXACT_ROWS)
    golden = mask_dispatch_rows(_EXACT_ROWS, num_partitions=_num_parts(dut), mode=DispatchMode.EXACT_BRANCH)
    assert parts == golden
    assert parts[0] == [(1, 100, 10, 500_000)]  # branch 1 -> lane 0
    assert parts[1] == [(2, 95, 2, 250_000), (2, 77, 5, 42)]


@cocotb.test()
async def exact_branch_all_dropped_closes_out_every_lane(dut):
    # a batch of only branch-0 rows: every lane is a zero-row success close-out
    await _start(dut)
    rows = [(0, i, i, i) for i in range(1, 8)]
    parts = await _run(dut, rows)
    assert parts == [[] for _ in range(_num_parts(dut))]


@cocotb.test()
async def exact_branch_backpressure_holds_rows_stable(dut):
    await _start(dut)
    parts = await _run(dut, _EXACT_ROWS, ready_pattern=(1, 0, 0, 1, 0))
    assert parts == mask_dispatch_rows(_EXACT_ROWS, num_partitions=_num_parts(dut), mode=DispatchMode.EXACT_BRANCH)


@cocotb.test()
async def exact_branch_repeated_batches_back_to_back(dut):
    await _start(dut)
    rng = random.Random(11)
    for _ in range(4):
        count = rng.randint(1, 30)
        rows = [(rng.randint(-2, 6), rng.randint(-1000, 1000), rng.randint(0, 1000), rng.randint(0, 1_000_000)) for _ in range(count)]
        parts = await _run(dut, rows)
        assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut), mode=DispatchMode.EXACT_BRANCH)


@cocotb.test()
async def exact_branch_drop_dominant_sustains_front_rate(dut):
    # THE q19 PROPERTY (wide front only): with the dominant class dropped at
    # the router, the front accepts ~1 row/cycle even though the lanes drain
    # two-beat — dropped rows never queue behind a lane skid
    if not _wide(dut):
        return
    await _start(dut)
    num_parts = _num_parts(dut)
    rows = []
    for i in range(96):
        rows.append((0, i, i, 0) if i % 8 else ((i // 8) % num_parts + 1, i, i, 0))
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
        await RisingEdge(dut.clk)
        if accepts == len(rows):
            break
    await send
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    assert accepts == len(rows), f"only {accepts}/{len(rows)} rows accepted"
    window = last_accept - first_accept + 1
    assert window <= len(rows) + 4, f"front took {window} cycles for {len(rows)} drop-dominant rows (not ~1 row/cycle)"

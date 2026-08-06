"""MODE == DispatchMode.HASH: dest = the top log2(N) bits of the low 32
bits of key * 0x9E3779B9. The multiplier and bit-extraction are contract —
a row's partition is part of the answer — so this bench proves the HDL
computes the identical partition the golden does, key for key, including
the strided keys MASK mode crowds onto a fraction of the lanes.

Runner passes parameters={"MODE": 2, "NUM_PARTITIONS": 4} at either
IN_WIDTH; the width is detected from the dut so one module covers both
fronts."""

from __future__ import annotations

import random

import cocotb

from dau_core.operators import DispatchMode, mask_dispatch_rows
from dau_core.tests.cocotb_benches.dau_int32_key_mask_dispatcher_tb import (
    _drive_and_collect,
    _num_parts,
    _send_rows,
)
from dau_core.tests.cocotb_benches.dau_int32_key_mask_dispatcher_wide_tb import (
    _send_row_beats,
    _start,
)

# keys sharing a stride of 4: MASK reads their low bits, so they reach only
# every 4th lane (lane 0 alone when N <= 4) — the failure HASH mode fixes
_STRIDED_ROWS = [(key * 4, key + 100, key, key * 7) for key in range(24)]

# adversarial keys: zero, negatives (whose unsigned image is large), the
# int32 extremes, and keys congruent modulo a power of two
_MIXED_ROWS = [
    (0, 1, 2, 3),
    (-1, 4, 5, 6),
    (2147483647, 7, 8, 9),
    (-2147483648, 10, 11, 12),
    (1, 13, 14, 15),
    (65537, 16, 17, 18),
    (-7, 19, 20, 21),
    (256, 22, 23, 24),
]


def _wide(dut) -> bool:
    return len(dut.input_data) == 128


async def _send(dut, rows) -> None:
    if _wide(dut):
        await _send_row_beats(dut, rows)
    else:
        await _send_rows(dut, rows)


async def _run(dut, rows, *, ready_pattern=(1,)):
    golden = mask_dispatch_rows(rows, num_partitions=_num_parts(dut), mode=DispatchMode.HASH)
    send = cocotb.start_soon(_send(dut, rows))
    parts = await _drive_and_collect(dut, golden, ready_pattern)
    await send
    return parts


@cocotb.test()
async def hash_dispatch_matches_the_golden_partition_for_partition(dut):
    await _start(dut)
    parts = await _run(dut, _MIXED_ROWS)
    assert parts == mask_dispatch_rows(_MIXED_ROWS, num_partitions=_num_parts(dut), mode=DispatchMode.HASH)


@cocotb.test()
async def hash_dispatch_spreads_strided_keys(dut):
    """The motivating case: MASK crowds strided keys onto a fraction of
    the lanes; HASH mixes the whole key and reaches more of them."""
    await _start(dut)
    parts = await _run(dut, _STRIDED_ROWS)
    golden = mask_dispatch_rows(_STRIDED_ROWS, num_partitions=_num_parts(dut), mode=DispatchMode.HASH)
    assert parts == golden
    masked = mask_dispatch_rows(_STRIDED_ROWS, num_partitions=_num_parts(dut), mode=DispatchMode.MASK)
    if _num_parts(dut) > 1:
        # MASK reads the key's low bits, so a stride of s reaches only
        # every s-th lane (all of them when s >= N); HASH reaches more.
        # Comparing lane OCCUPANCY is the property that holds at every N,
        # where "MASK puts everything on lane 0" holds only for s >= N.
        assert sum(1 for part in masked if part) < sum(1 for part in parts if part), (
            [len(part) for part in masked],
            [len(part) for part in parts],
        )


@cocotb.test()
async def hash_dispatch_drops_nothing(dut):
    await _start(dut)
    parts = await _run(dut, _MIXED_ROWS)
    assert sum(len(part) for part in parts) == len(_MIXED_ROWS)


@cocotb.test()
async def hash_dispatch_keeps_a_key_on_one_lane_in_order(dut):
    await _start(dut)
    rows = [(key % 5, index, 0, 0) for index, key in enumerate(range(40))]
    parts = await _run(dut, rows)
    assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut), mode=DispatchMode.HASH)
    for key in range(5):
        lanes = {index for index, part in enumerate(parts) if any(row[0] == key for row in part)}
        assert len(lanes) <= 1, key
    for part in parts:
        assert [row[1] for row in part] == sorted(row[1] for row in part)


@cocotb.test()
async def hash_dispatch_backpressure_holds_rows_stable(dut):
    await _start(dut)
    parts = await _run(dut, _MIXED_ROWS, ready_pattern=(1, 0, 0, 1, 0))
    assert parts == mask_dispatch_rows(_MIXED_ROWS, num_partitions=_num_parts(dut), mode=DispatchMode.HASH)


@cocotb.test()
async def hash_dispatch_repeated_batches_back_to_back(dut):
    await _start(dut)
    rng = random.Random(23)
    for _ in range(4):
        count = rng.randint(1, 30)
        rows = [(rng.randint(-1 << 20, 1 << 20), rng.randint(0, 999), 0, 0) for _ in range(count)]
        parts = await _run(dut, rows)
        assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut), mode=DispatchMode.HASH)

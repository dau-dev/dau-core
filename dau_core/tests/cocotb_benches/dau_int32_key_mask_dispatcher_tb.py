from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import mask_dispatch_rows
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

ERR_STREAM = 2


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    num_parts = _num_parts(dut)
    rows = [(index % num_parts, index, index + 1, index + 2) for index in range(256)]
    await assert_sustained_ingest(
        dut,
        "int32-key-mask-dispatcher",
        send=lambda: _send_rows(dut, rows),
        input_beats=2 * len(rows),
        beats_per_row=2,
        params={"IN_WIDTH": 64},
    )


_MASK32 = 0xFFFF_FFFF
_MASK64 = 0xFFFF_FFFF_FFFF_FFFF

# (key, value, aux0, aux1); keys spread across the 4 mask lanes (key & 3),
# including negative keys (two's-complement low bits: -1 -> lane 3)
_ROWS = [
    (4, 100, 10, 500_000),
    (1, 105, 3, 0),
    (6, 95, 2, 250_000),
    (3, 102, 1, 999_999),
    (-1, 102, 7, 0),
    (8, 99, 4, 100_000),
    (-4, 88, 9, 1),
]


@cocotb.test()
async def dispatch_routes_rows_by_key_mask(dut):
    await _start(dut)
    parts = await _run(dut, _ROWS)
    assert parts == mask_dispatch_rows(_ROWS, num_partitions=_num_parts(dut))


@cocotb.test()
async def negative_keys_mask_two_complement(dut):
    await _start(dut)
    rows = [(-1, 1, 1, 0), (-4, 2, 2, 0), (-3, 3, 3, 0), (2, 4, 4, 0)]
    parts = await _run(dut, rows)
    assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut))
    if _num_parts(dut) == 4:
        assert parts[3] == [(-1, 1, 1, 0)]  # -1 & 3 == 3
        assert parts[0] == [(-4, 2, 2, 0)]  # -4 & 3 == 0
        assert parts[1] == [(-3, 3, 3, 0)]  # -3 & 3 == 1


@cocotb.test()
async def all_rows_to_one_partition(dut):
    # a same-lane run: every other lane is a zero-row success close-out
    await _start(dut)
    rows = [(4 * i + 2, i, i, 0) for i in range(1, 6)]  # all key & 3 == 2
    parts = await _run(dut, rows)
    assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut))
    if _num_parts(dut) == 4:
        assert parts[2] == rows


@cocotb.test()
async def torn_row_errors_every_lane(dut):
    # input_last on a w0 is a torn row: every lane reports ERR_STREAM and the
    # dispatcher recovers for the next batch
    await _start(dut)
    await _drive_torn_first_row(dut)
    statuses = await _collect_all_status(dut)
    for p, entry in enumerate(statuses):
        error, code = entry
        assert error == 1 and code == ERR_STREAM, f"lane {p}: {entry}"
    parts = await _run(dut, _ROWS)
    assert parts == mask_dispatch_rows(_ROWS, num_partitions=_num_parts(dut))


@cocotb.test()
async def backpressure_holds_rows_stable(dut):
    await _start(dut)
    parts = await _run(dut, _ROWS, ready_pattern=(1, 0, 0, 1, 0))
    assert parts == mask_dispatch_rows(_ROWS, num_partitions=_num_parts(dut))


@cocotb.test()
async def repeated_batches_back_to_back(dut):
    await _start(dut)
    for _ in range(3):
        parts = await _run(dut, _ROWS)
        assert parts == mask_dispatch_rows(_ROWS, num_partitions=_num_parts(dut))


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0xD15C)
    for trial in range(25):
        rows = _random_rows(rng)
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):
            pattern = pattern + (1,)
        parts = await _run(dut, rows, ready_pattern=pattern)
        assert parts == mask_dispatch_rows(rows, num_partitions=_num_parts(dut)), f"trial {trial}: rows={rows}"


# ---- helpers ----


def _num_parts(dut) -> int:
    return len(dut.output_valid)


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _random_rows(rng: random.Random) -> list[tuple[int, int, int, int]]:
    count = rng.randint(1, 40)
    return [(rng.randint(-50, 50), rng.randint(-1000, 1000), rng.randint(0, 1000), rng.randint(0, 1_000_000)) for _ in range(count)]


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


async def _run(dut, rows, *, ready_pattern=(1,)):
    golden = mask_dispatch_rows(rows, num_partitions=_num_parts(dut))
    send = cocotb.start_soon(_send_rows(dut, rows))
    parts = await _drive_and_collect(dut, golden, ready_pattern)
    await send
    return parts


async def _send_rows(dut, rows) -> None:
    words = []
    for key, value, aux0, aux1 in rows:
        words.append(((value & _MASK32) << 32) | (key & _MASK32))
        words.append(((aux1 & _MASK32) << 32) | (aux0 & _MASK32))
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


async def _drive_torn_first_row(dut) -> None:
    dut.input_data.value = (100 << 32) | 5
    dut.input_last.value = 1  # last on a w0 == torn row
    dut.input_valid.value = 1
    while True:
        await ReadOnly()
        fired = int(dut.input_ready.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _drive_and_collect(dut, golden, ready_pattern):
    """Drive all partition output/status streams and reassemble per-partition
    rows (phase-shifted ready per lane so lanes stall out of step). A lane
    expecting rows completes on its last word; a zero-row lane on its success
    status. Afterwards the streams must fall quiescent."""
    num_parts = len(golden)
    words: list[list[int]] = [[] for _ in range(num_parts)]
    expect_output = [len(golden[p]) != 0 for p in range(num_parts)]
    done = [False] * num_parts

    for pattern_index in range(20000):
        oready = 0
        for p in range(num_parts):
            if expect_output[p] and not done[p]:
                bit = ready_pattern[(pattern_index + p) % len(ready_pattern)]
                oready |= bit << p
        dut.output_ready.value = oready
        dut.status_ready.value = (1 << num_parts) - 1

        await ReadOnly()
        ov = int(dut.output_valid.value)
        od = int(dut.output_data.value)
        ol = int(dut.output_last.value)
        sv = int(dut.status_valid.value)
        se = int(dut.status_error.value)
        captured = [(p, (od >> (p * 64)) & _MASK64, (ol >> p) & 1) for p in range(num_parts) if (oready >> p) & 1 and (ov >> p) & 1]
        status_hit = [(p, (se >> p) & 1) for p in range(num_parts) if not expect_output[p] and not done[p] and (sv >> p) & 1]

        await RisingEdge(dut.clk)

        for p, word, last in captured:
            words[p].append(word)
            if last:
                done[p] = True
        for p, error in status_hit:
            assert error == 0, f"empty partition {p} raised an error status"
            done[p] = True

        if all(done):
            await _assert_quiescent(dut, num_parts)
            return [_unpack_rows(words[p]) for p in range(num_parts)]

    raise AssertionError(f"timed out; done={done} collected={[len(w) for w in words]}")


async def _assert_quiescent(dut, num_parts: int) -> None:
    dut.output_ready.value = (1 << num_parts) - 1
    dut.status_ready.value = (1 << num_parts) - 1
    for _ in range(6):
        await ReadOnly()
        assert int(dut.output_valid.value) == 0, "output beat after batch completion"
        assert int(dut.status_valid.value) == 0, "status beat after batch completion"
        await RisingEdge(dut.clk)
    dut.output_ready.value = 0
    dut.status_ready.value = 0


async def _collect_all_status(dut, *, only: set[int] | None = None):
    num_parts = _num_parts(dut)
    targets = only if only is not None else set(range(num_parts))
    result: list[tuple[int, int] | None] = [None] * num_parts
    dut.status_ready.value = (1 << num_parts) - 1
    for _ in range(512):
        await ReadOnly()
        sv = int(dut.status_valid.value)
        se = int(dut.status_error.value)
        sc = int(dut.status_error_code.value)
        hits = [(p, (se >> p) & 1, (sc >> (p * 8)) & 0xFF) for p in targets if result[p] is None and (sv >> p) & 1]
        await RisingEdge(dut.clk)
        for p, error, code in hits:
            result[p] = (error, code)
        if all(result[p] is not None for p in targets):
            dut.status_ready.value = 0
            return result
    raise AssertionError(f"timed out waiting for status; got {result}")


def _unpack_rows(words: list[int]) -> list[tuple[int, int, int, int]]:
    rows = []
    for offset in range(0, len(words), 2):
        w0, w1 = words[offset], words[offset + 1]
        rows.append((_signed32(w0 & _MASK32), _signed32(w0 >> 32), _signed32(w1 & _MASK32), _signed32(w1 >> 32)))
    return rows

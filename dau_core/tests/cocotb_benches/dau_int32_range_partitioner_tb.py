from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import range_partition_bar_rows

ERR_STREAM = 2
_MASK32 = 0xFFFF_FFFF
_MASK64 = 0xFFFF_FFFF_FFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# (key, price, shares, frac); keys chosen to spread across splitters [10, 20, 30]
_ROWS = [
    (5, 100, 10, 500_000),
    (10, 105, 3, 0),
    (25, 95, 2, 250_000),
    (35, 102, 1, 999_999),
    (19, 102, 7, 0),
    (30, 99, 4, 100_000),
    (-3, 88, 9, 1),
]
_SPLITTERS = [10, 20, 30]  # a 4-partition (default) configuration


# ---- targeted tests (default NUM_PARTITIONS == 4) ----


@cocotb.test()
async def partition_routes_rows_by_range(dut):
    await _start(dut)
    if _num_parts(dut) != 4:
        return
    parts = await _run(dut, _ROWS, _SPLITTERS)
    assert parts == range_partition_bar_rows(_ROWS, splitters=_SPLITTERS)


@cocotb.test()
async def boundary_key_lands_in_upper_partition(dut):
    # a key exactly equal to a splitter routes to the upper partition (>=)
    await _start(dut)
    if _num_parts(dut) != 4:
        return
    rows = [(10, 1, 1, 0), (20, 2, 2, 0), (30, 3, 3, 0), (9, 4, 4, 0)]
    parts = await _run(dut, rows, _SPLITTERS)
    assert parts == range_partition_bar_rows(rows, splitters=_SPLITTERS)
    assert parts[0] == [(9, 4, 4, 0)]  # only key 9 is < 10
    assert parts[1] == [(10, 1, 1, 0)]  # key 10 -> partition 1
    assert parts[3] == [(30, 3, 3, 0)]  # key 30 -> top partition


@cocotb.test()
async def all_rows_to_one_partition(dut):
    await _start(dut)
    if _num_parts(dut) != 4:
        return
    rows = [(25, i, i, 0) for i in range(1, 6)]  # all in [20, 30)
    parts = await _run(dut, rows, _SPLITTERS)
    assert parts[2] == rows
    assert parts[0] == [] and parts[1] == [] and parts[3] == []


@cocotb.test()
async def negative_keys_partition_signed(dut):
    await _start(dut)
    if _num_parts(dut) != 4:
        return
    splitters = [-10, 0, 10]
    rows = [(-20, 1, 1, 0), (-10, 2, 2, 0), (-1, 3, 3, 0), (0, 4, 4, 0), (50, 5, 5, 0)]
    parts = await _run(dut, rows, splitters)
    assert parts == range_partition_bar_rows(rows, splitters=splitters)
    assert parts[0] == [(-20, 1, 1, 0)]  # < -10


@cocotb.test()
async def last_lands_on_each_partitions_final_row(dut):
    # multiple rows per partition: each partition's stream ends with last on its
    # own final forwarded row (verified structurally by _drive_and_collect)
    await _start(dut)
    if _num_parts(dut) != 4:
        return
    rows = [(5, 1, 1, 0), (5, 2, 2, 0), (25, 3, 3, 0), (25, 4, 4, 0), (25, 5, 5, 0)]
    parts = await _run(dut, rows, _SPLITTERS)
    assert parts[0] == [(5, 1, 1, 0), (5, 2, 2, 0)]
    assert parts[2] == [(25, 3, 3, 0), (25, 4, 4, 0), (25, 5, 5, 0)]


# ---- N-agnostic tests (run for any NUM_PARTITIONS) ----


@cocotb.test()
async def int32_extreme_keys_partition_signed(dut):
    await _start(dut)
    num_parts = _num_parts(dut)
    # splitters spanning the signed extremes; keys at the boundaries
    splitters = _spread_splitters(_INT32_MIN + 1, _INT32_MAX - 1, num_parts - 1)
    rows = [
        (_INT32_MIN, 1, 1, 0),
        (_INT32_MAX, 2, 2, 0),
        (0, 3, 3, 0),
        (-1, 4, 4, 0),
        (splitters[0] if splitters else 0, 5, 5, 0),
    ]
    parts = await _run(dut, rows, splitters)
    assert parts == range_partition_bar_rows(rows, splitters=splitters)


@cocotb.test()
async def backpressure_on_every_stream(dut):
    await _start(dut)
    parts = await _run(dut, _ROWS[: 2 * _num_parts(dut)], _default_splitters(dut), ready_pattern=(1, 0, 0, 1, 0))
    golden = range_partition_bar_rows(_ROWS[: 2 * _num_parts(dut)], splitters=_default_splitters(dut))
    assert parts == golden


@cocotb.test()
async def torn_row_status_survives_backpressure(dut):
    # a torn first row (input_last on a w0): every partition must raise a stable
    # ERR_STREAM that persists under status backpressure until it is consumed.
    await _start(dut)
    num_parts = _num_parts(dut)
    _set_splitters(dut, _default_splitters(dut))
    await _drive_torn_first_row(dut)

    # hold status_ready low: the error status must latch (a cycle or two after
    # the front enters its error drain) and then stay stable
    dut.status_ready.value = 0
    found = False
    for _ in range(8):
        await ReadOnly()
        found = int(dut.status_valid.value) == (1 << num_parts) - 1
        await RisingEdge(dut.clk)
        if found:
            break
    assert found, "error status never asserted on all lanes"
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == (1 << num_parts) - 1, "error status not held on all lanes"
        assert int(dut.status_error.value) == (1 << num_parts) - 1
        # the front must not accept a new batch while status is pending
        assert int(dut.input_ready.value) == 0, "front advanced before error status drained"
        await RisingEdge(dut.clk)

    codes = await _collect_all_status(dut)
    for part, (error, code) in enumerate(codes):
        assert error == 1 and code == ERR_STREAM, f"partition {part}: error={error} code={code}"

    # recovers: a normal batch partitions correctly afterward
    parts = await _run(dut, _ROWS[: 2 * num_parts], _default_splitters(dut))
    assert parts == range_partition_bar_rows(_ROWS[: 2 * num_parts], splitters=_default_splitters(dut))


@cocotb.test()
async def status_backpressure_stalls_the_front(dut):
    # empty partitions raise a success status at batch end; while that status is
    # backpressured the front must stay in its drain state and refuse new input
    # (the regression the finding-1 fix prevents).
    await _start(dut)
    num_parts = _num_parts(dut)
    if num_parts < 2:
        return
    # splitters so that only partition 0 receives rows; the rest stay empty
    splitters = list(range(1, num_parts))  # keys < 1 -> partition 0
    _set_splitters(dut, splitters)
    rows = [(-5, 1, 1, 0), (-7, 2, 2, 0)]

    # drain partition 0's rows but hold every status stalled
    dut.status_ready.value = 0
    send = cocotb.start_soon(_send_rows(dut, rows))
    await _drain_partition_output(dut, 0)
    await send

    # the empty lanes now hold a pending success status; the front is stuck
    for _ in range(6):
        await ReadOnly()
        pending = int(dut.status_valid.value)
        assert pending == ((1 << num_parts) - 1) ^ 1, f"empty lanes should hold status, got {pending:#x}"
        assert int(dut.status_error.value) == 0, "empty-lane close-out must be success"
        assert int(dut.input_ready.value) == 0, "front accepted input while status pending"
        await RisingEdge(dut.clk)

    # release status: the front frees and the next batch partitions normally
    codes = await _collect_all_status(dut, only=set(range(1, num_parts)))
    for part in range(1, num_parts):
        assert codes[part] == (0, 0), f"partition {part} close-out {codes[part]}"
    parts = await _run(dut, _ROWS[: 2 * num_parts], _default_splitters(dut))
    assert parts == range_partition_bar_rows(_ROWS[: 2 * num_parts], splitters=_default_splitters(dut))


@cocotb.test()
async def constrained_random_streams_match_golden(dut):
    await _start(dut)
    num_parts = _num_parts(dut)
    rng = random.Random(0xDA0)
    for trial in range(60):
        splitters = _random_monotonic_splitters(rng, num_parts - 1)
        rows = _random_rows(rng)
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):  # a ready pattern must let data through eventually
            pattern = pattern + (1,)
        parts = await _run(dut, rows, splitters, ready_pattern=pattern)
        assert parts == range_partition_bar_rows(rows, splitters=splitters), f"trial {trial}: splitters={splitters} rows={rows}"


# ---- helpers ----


def _num_parts(dut) -> int:
    return len(dut.output_valid)


def _default_splitters(dut) -> list[int]:
    return _spread_splitters(-30, 30, _num_parts(dut) - 1)


def _spread_splitters(lo: int, hi: int, count: int) -> list[int]:
    if count <= 0:
        return []
    step = (hi - lo) // (count + 1)
    return [lo + step * (i + 1) for i in range(count)]


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _random_monotonic_splitters(rng: random.Random, count: int) -> list[int]:
    return sorted(rng.randint(-40, 40) for _ in range(count))


def _random_rows(rng: random.Random) -> list[tuple[int, int, int, int]]:
    # >= 1 row: a batch signals its end with input_last on its final row, so a
    # zero-word stream has no batch boundary (same contract as the pair-key
    # filter). Empty partitions *within* a batch are exercised separately.
    count = rng.randint(1, 40)
    return [(rng.randint(-50, 50), rng.randint(-1000, 1000), rng.randint(0, 1000), rng.randint(0, 1_000_000)) for _ in range(count)]


def _set_splitters(dut, splitters: list[int]) -> None:
    assert len(splitters) == _num_parts(dut) - 1
    packed = 0
    for index, value in enumerate(splitters):
        packed |= (value & _MASK32) << (index * 32)
    dut.cfg_splitters.value = packed


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.cfg_splitters.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, splitters, *, ready_pattern=(1,)):
    _set_splitters(dut, splitters)
    golden = range_partition_bar_rows(rows, splitters=splitters)
    send = cocotb.start_soon(_send_rows(dut, rows))
    parts = await _drive_and_collect(dut, golden, ready_pattern)
    await send
    return parts


async def _send_rows(dut, rows) -> None:
    words = []
    for key, price, shares, frac in rows:
        words.append(((price & _MASK32) << 32) | (key & _MASK32))
        words.append(((frac & _MASK32) << 32) | (shares & _MASK32))
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
    rows. Each active partition gets an independent (phase-shifted) ready
    pattern so lanes stall out of step. A partition that expects rows completes
    on its last word; a partition that expects zero rows completes on its
    success status. After completion the streams must fall quiescent."""
    num_parts = len(golden)
    words: list[list[int]] = [[] for _ in range(num_parts)]
    expect_output = [len(golden[p]) != 0 for p in range(num_parts)]
    done = [False] * num_parts
    pattern_index = 0

    for _ in range(20000):
        oready = 0
        for p in range(num_parts):
            if expect_output[p] and not done[p]:
                bit = ready_pattern[(pattern_index + p) % len(ready_pattern)]
                oready |= bit << p
        pattern_index += 1
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
    """After a batch completes, nothing more may appear on any output or status
    stream even with every ready held high (no trailing/duplicated beats)."""
    dut.output_ready.value = (1 << num_parts) - 1
    dut.status_ready.value = (1 << num_parts) - 1
    for _ in range(6):
        await ReadOnly()
        assert int(dut.output_valid.value) == 0, "output beat after batch completion"
        assert int(dut.status_valid.value) == 0, "status beat after batch completion"
        await RisingEdge(dut.clk)
    dut.output_ready.value = 0
    dut.status_ready.value = 0


async def _drain_partition_output(dut, partition: int) -> None:
    # drain a partition's stream to (and including) its final row — only that
    # row carries last=1, which marks the partition's stream end
    dut.output_ready.value = 1 << partition
    for _ in range(4096):
        await ReadOnly()
        fired = (int(dut.output_valid.value) >> partition) & 1
        last = (int(dut.output_last.value) >> partition) & 1
        await RisingEdge(dut.clk)
        if fired and last:
            dut.output_ready.value = 0
            return
    raise AssertionError(f"partition {partition}: last never drained")


async def _collect_all_status(dut, *, only: set[int] | None = None):
    """Wait for a status beat from every (or ``only``) partition; return
    (error, code) per partition (None for partitions not collected)."""
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

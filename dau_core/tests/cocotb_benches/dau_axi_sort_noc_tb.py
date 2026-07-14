from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import SorterErrorCode, range_partition_bar_rows, sort_bar_rows_by_key

NUM_LANES = 4
SORT_CAPACITY = 16
INPUT_ADDRESS = 0x0000_0000
LANE_OUTPUT_ADDRESSES = tuple(0x0004_0000 + lane * 0x0000_4000 for lane in range(NUM_LANES))

ERR_JOB_LENGTH = 0xFE

_SPLITTERS = [10, 20, 30]

_ROWS = [
    # keys spread across the four ranges (<10, [10,20), [20,30), >=30)
    (25, 95, 2, 250_000),
    (5, 100, 10, 500_000),
    (35, 102, 1, 999_999),
    (10, 105, 3, 0),
    (-3, 88, 9, 1),
    (30, 99, 4, 100_000),
    (19, 102, 7, 0),
    (25, 44, 5, 2),
    (12, 46, 5, 3),
]


def _golden_lanes(rows, splitters):
    return [sort_bar_rows_by_key(part) for part in range_partition_bar_rows(rows, splitters=splitters)]


@cocotb.test()
async def sorted_scan_matches_golden(dut):
    await _start(dut)
    lanes = await _run_job(dut, _ROWS, _SPLITTERS)
    golden = _golden_lanes(_ROWS, _SPLITTERS)
    for lane in range(NUM_LANES):
        assert lanes[lane] == golden[lane], f"lane {lane} mismatch"
    # ordered partitions + stable local sorts: the concatenation IS the
    # globally sorted scan (sort-by-concat, no merge)
    flattened = [row for lane_rows in lanes for row in lane_rows]
    assert flattened == sort_bar_rows_by_key(_ROWS)


@cocotb.test()
async def empty_lane_closes_out(dut):
    await _start(dut)
    rows = [(5, 1, 1, 0), (15, 2, 2, 0), (35, 3, 3, 0)]  # [20,30) gets nothing
    lanes = await _run_job(dut, rows, _SPLITTERS)
    golden = _golden_lanes(rows, _SPLITTERS)
    assert lanes[2] == []
    assert lanes == golden


@cocotb.test()
async def multi_burst_sorted_scan(dut):
    # more rows than one AXI burst, keys balanced so no lane exceeds the
    # sorter capacity; descending keys within each range stress the sort
    await _start(dut)
    rows = []
    for i in range(SORT_CAPACITY):
        rows.append((9 - (i % 10), 100 + i, i, 0))  # lane 0: keys < 10
        rows.append((19 - (i % 10), 200 + i, i, 1))  # lane 1: [10, 20)
        rows.append((29 - (i % 10), 300 + i, i, 2))  # lane 2: [20, 30)
        rows.append((39 - (i % 10), 400 + i, i, 3))  # lane 3: >= 30
    lanes = await _run_job(dut, rows, _SPLITTERS)
    golden = _golden_lanes(rows, _SPLITTERS)
    for lane in range(NUM_LANES):
        assert lanes[lane] == golden[lane], f"lane {lane} mismatch"
    flattened = [row for lane_rows in lanes for row in lane_rows]
    assert flattened == sort_bar_rows_by_key(rows)


@cocotb.test()
async def lane_overflow_reports_capacity_error_then_recovers(dut):
    # one partition larger than the sorter capacity: the job must error with
    # the sorter's ERR_CAPACITY (partition finer), then run clean again
    await _start(dut)
    rows = [(5, i, i, 0) for i in range(SORT_CAPACITY + 1)]  # all in lane 0
    words = _pack_words(rows)
    await _backdoor_load(dut, INPUT_ADDRESS // 8, words)
    _set_splitters(dut, _SPLITTERS)
    await _kick_job(dut, len(words) * 8)
    await _wait_done(dut)
    assert int(dut.error.value) == 1
    assert int(dut.error_code.value) == SorterErrorCode.CAPACITY

    lanes = await _run_job(dut, _ROWS, _SPLITTERS)
    assert lanes == _golden_lanes(_ROWS, _SPLITTERS)


@cocotb.test()
async def repeated_jobs(dut):
    await _start(dut)
    first = await _run_job(dut, _ROWS, _SPLITTERS)
    second = await _run_job(dut, [(25, 7, 1, 0)], _SPLITTERS)
    assert second[2] == [(25, 7, 1, 0)]
    assert second[0] == [] and second[1] == [] and second[3] == []
    third = await _run_job(dut, _ROWS, _SPLITTERS)
    assert third == first


@cocotb.test()
async def misaligned_length_errors_then_recovers(dut):
    await _start(dut)
    _set_splitters(dut, _SPLITTERS)
    await _kick_job(dut, 24)  # not on the 16-byte row grid
    await _wait_done(dut)
    assert int(dut.error.value) == 1
    assert int(dut.error_code.value) == ERR_JOB_LENGTH

    lanes = await _run_job(dut, _ROWS, _SPLITTERS)
    assert lanes == _golden_lanes(_ROWS, _SPLITTERS)


@cocotb.test()
async def randomized_jobs_match_golden(dut):
    await _start(dut)
    rng = random.Random(0x50B7)
    for trial in range(10):
        splitters = sorted(rng.randint(-20, 20) for _ in range(NUM_LANES - 1))
        # rejection-sample until no lane exceeds the sorter capacity
        for _ in range(100):
            count = rng.randint(1, NUM_LANES * SORT_CAPACITY // 2)
            rows = [(rng.randint(-30, 30), rng.randint(-500, 500), rng.randint(0, 100), rng.randint(0, 9)) for _ in range(count)]
            if all(len(part) <= SORT_CAPACITY for part in range_partition_bar_rows(rows, splitters=splitters)):
                break
        lanes = await _run_job(dut, rows, splitters)
        golden = _golden_lanes(rows, splitters)
        assert lanes == golden, f"trial {trial}: splitters={splitters} rows={rows}"
        flattened = [row for lane_rows in lanes for row in lane_rows]
        assert flattened == sort_bar_rows_by_key(rows)


# ---- helpers ----


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.start.value = 0
    dut.input_address.value = 0
    dut.input_length_bytes.value = 0
    dut.cfg_splitters.value = 0
    packed_outputs = 0
    for lane, address in enumerate(LANE_OUTPUT_ADDRESSES):
        packed_outputs |= address << (32 * lane)
    dut.lane_output_address.value = packed_outputs
    dut.bd_write.value = 0
    dut.bd_index.value = 0
    dut.bd_wdata.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


def _set_splitters(dut, splitters) -> None:
    packed = 0
    for index, value in enumerate(splitters):
        packed |= (value & 0xFFFF_FFFF) << (index * 32)
    dut.cfg_splitters.value = packed


def _pack_words(rows) -> list[int]:
    words = []
    for key, price, shares, frac in rows:
        words.append(((price & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF))
        words.append(((frac & 0xFFFFFFFF) << 32) | (shares & 0xFFFFFFFF))
    return words


async def _run_job(dut, rows, splitters):
    words = _pack_words(rows)
    await _backdoor_load(dut, INPUT_ADDRESS // 8, words)
    _set_splitters(dut, splitters)
    await _kick_job(dut, len(words) * 8)
    await _wait_done(dut)
    assert int(dut.error.value) == 0, f"job errored with code {int(dut.error_code.value)}"
    result_lengths = _u32_lanes(int(dut.lane_result_length_bytes.value))
    lanes = []
    for lane in range(NUM_LANES):
        assert result_lengths[lane] % 16 == 0, "sorted rows are 16 bytes"
        record_words = await _backdoor_read(dut, LANE_OUTPUT_ADDRESSES[lane] // 8, result_lengths[lane] // 8)
        lanes.append([_decode_row(record_words[offset : offset + 2]) for offset in range(0, len(record_words), 2)])
    return lanes


async def _kick_job(dut, input_length_bytes: int) -> None:
    dut.input_address.value = INPUT_ADDRESS
    dut.input_length_bytes.value = input_length_bytes
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    await RisingEdge(dut.clk)


async def _wait_done(dut) -> None:
    for _ in range(60_000):
        await ReadOnly()
        if int(dut.done.value) == 1:
            await RisingEdge(dut.clk)
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for job done")


async def _backdoor_load(dut, base_index: int, words: list[int]) -> None:
    for offset, word in enumerate(words):
        dut.bd_index.value = base_index + offset
        dut.bd_wdata.value = word
        dut.bd_write.value = 1
        await RisingEdge(dut.clk)
    dut.bd_write.value = 0
    await RisingEdge(dut.clk)


async def _backdoor_read(dut, base_index: int, count: int) -> list[int]:
    words = []
    for offset in range(count):
        dut.bd_index.value = base_index + offset
        await ReadOnly()
        words.append(int(dut.bd_rdata.value))
        await RisingEdge(dut.clk)
    return words


def _u32_lanes(packed: int) -> list[int]:
    return [(packed >> (32 * lane)) & 0xFFFF_FFFF for lane in range(NUM_LANES)]


def _decode_row(words: list[int]) -> tuple[int, int, int, int]:
    w0, w1 = words
    return (_signed32(w0 & 0xFFFFFFFF), _signed32(w0 >> 32), _signed32(w1 & 0xFFFFFFFF), _signed32(w1 >> 32))


def _signed32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value

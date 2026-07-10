from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import BarAggregate, bar_aggregate_rows, filter_bar_rows_by_key

NUM_LANES = 4
INPUT_ADDRESS = 0x0000_0000
LANE_OUTPUT_ADDRESSES = tuple(0x0004_0000 + lane * 0x0000_4000 for lane in range(NUM_LANES))

ERR_JOB_LENGTH = 0xFE

_TRADES = [
    # keys spread across all four partitions (key & 3)
    (568, 100, 10, 500_000),
    (568, 105, 3, 0),
    (569, 95, 2, 250_000),
    (570, 102, 1, 999_999),
    (570, 102, 7, 0),
    (571, 99, 4, 100_000),
    (572, -3, 0, 1),
    (573, 44, 5, 2),
    (573, 46, 5, 3),
]


@cocotb.test()
async def noc_partitions_match_golden(dut):
    await _start(dut)
    lanes = await _run_job(dut, _TRADES)
    for lane in range(NUM_LANES):
        golden = bar_aggregate_rows(filter_bar_rows_by_key(_TRADES, key_mask=NUM_LANES - 1, key_match=lane))
        assert lanes[lane] == golden, f"lane {lane} mismatch"


@cocotb.test()
async def noc_empty_lane_closes_out(dut):
    await _start(dut)
    rows = [(568, 10, 1, 0), (569, 11, 1, 0), (570, 12, 1, 0)]  # lane 3 (key&3==3) gets nothing
    lanes = await _run_job(dut, rows)
    assert lanes[3] == []
    for lane in range(3):
        golden = bar_aggregate_rows(filter_bar_rows_by_key(rows, key_mask=NUM_LANES - 1, key_match=lane))
        assert lanes[lane] == golden


@cocotb.test()
async def noc_multi_burst_batch(dut):
    await _start(dut)
    rows = [(index // 5, 100 + (index % 11) - 5, 1 + index % 3, index % 7) for index in range(300)]
    lanes = await _run_job(dut, rows)
    for lane in range(NUM_LANES):
        golden = bar_aggregate_rows(filter_bar_rows_by_key(rows, key_mask=NUM_LANES - 1, key_match=lane))
        assert lanes[lane] == golden, f"lane {lane} mismatch"


@cocotb.test()
async def noc_deep_backpressure_batch(dut):
    # frequent bar close-outs stall every lane while the reader keeps many
    # bursts in flight: the regime where the hardware run lost records
    await _start(dut)
    rows = [(index // 3, 100 + (index % 13) - 6, 1 + index % 3, index % 5) for index in range(3000)]
    lanes = await _run_job(dut, rows)
    for lane in range(NUM_LANES):
        golden = bar_aggregate_rows(filter_bar_rows_by_key(rows, key_mask=NUM_LANES - 1, key_match=lane))
        assert lanes[lane] == golden, f"lane {lane} mismatch"


@cocotb.test()
async def noc_repeated_jobs(dut):
    await _start(dut)
    first = await _run_job(dut, _TRADES)
    second = await _run_job(dut, [(568, 7, 1, 0)])
    assert second[0] == [BarAggregate(key=568, open=7, close=7, low=7, high=7, shares=1, frac=0, count=1)]
    assert second[1] == [] and second[2] == [] and second[3] == []
    third = await _run_job(dut, _TRADES)
    assert third == first


@cocotb.test()
async def noc_misaligned_length_errors_then_recovers(dut):
    await _start(dut)
    # torn rows are unreachable here: the 16-byte grid is enforced before
    # any unit starts, so nothing is left waiting on a status
    await _kick_job(dut, 24)
    await _wait_done(dut)
    assert int(dut.error.value) == 1
    assert int(dut.error_code.value) == ERR_JOB_LENGTH

    lanes = await _run_job(dut, _TRADES)
    for lane in range(NUM_LANES):
        golden = bar_aggregate_rows(filter_bar_rows_by_key(_TRADES, key_mask=NUM_LANES - 1, key_match=lane))
        assert lanes[lane] == golden, f"lane {lane} mismatch after recovery"


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.start.value = 0
    dut.input_address.value = 0
    dut.input_length_bytes.value = 0
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


def _pack_words(rows) -> list[int]:
    words = []
    for key, price, shares, frac in rows:
        words.append(((price & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF))
        words.append(((frac & 0xFFFFFFFF) << 32) | (shares & 0xFFFFFFFF))
    return words


async def _run_job(dut, rows):
    words = _pack_words(rows)
    await _backdoor_load(dut, INPUT_ADDRESS // 8, words)
    await _kick_job(dut, len(words) * 8)
    await _wait_done(dut)
    assert int(dut.error.value) == 0, f"job errored with code {int(dut.error_code.value)}"
    result_lengths = _u32_lanes(int(dut.lane_result_length_bytes.value))
    bar_counts = _u64_lanes(int(dut.lane_bar_count.value))
    lanes = []
    for lane in range(NUM_LANES):
        assert result_lengths[lane] % 48 == 0, "bar records are 48 bytes"
        assert bar_counts[lane] == result_lengths[lane] // 48
        record_words = await _backdoor_read(dut, LANE_OUTPUT_ADDRESSES[lane] // 8, result_lengths[lane] // 8)
        lanes.append([_decode_bar(record_words[offset : offset + 6]) for offset in range(0, len(record_words), 6)])
    return lanes


async def _kick_job(dut, input_length_bytes: int) -> None:
    dut.input_address.value = INPUT_ADDRESS
    dut.input_length_bytes.value = input_length_bytes
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    await RisingEdge(dut.clk)


async def _wait_done(dut) -> None:
    for _ in range(30_000):
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


def _u64_lanes(packed: int) -> list[int]:
    return [(packed >> (64 * lane)) & 0xFFFF_FFFF_FFFF_FFFF for lane in range(NUM_LANES)]


def _decode_bar(words: list[int]) -> BarAggregate:
    return BarAggregate(
        key=_signed32(words[0] & 0xFFFFFFFF),
        open=_signed32(words[0] >> 32),
        close=_signed32(words[1] & 0xFFFFFFFF),
        low=_signed32(words[1] >> 32),
        high=_signed32(words[2] & 0xFFFFFFFF),
        shares=_signed64(words[3]),
        frac=_signed64(words[4]),
        count=words[5],
    )


def _signed32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


def _signed64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value

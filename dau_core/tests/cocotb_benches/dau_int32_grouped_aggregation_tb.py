from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import GroupAggregate, GroupedAggregationErrorCode, grouped_aggregate_rows

_OHLCV_ROWS = [
    (570, 100),
    (570, 105),
    (570, 95),
    (570, 102),  # bar 1
    (571, 102),
    (571, 99),  # bar 2
    (572, -3),  # bar 3 (single trade)
]


@cocotb.test()
async def grouped_ohlcv_bars_match_golden(dut):
    await _start(dut)
    groups = await _run_grouped(dut, _OHLCV_ROWS)
    assert groups == grouped_aggregate_rows(_OHLCV_ROWS)


@cocotb.test()
async def grouped_backpressure_on_both_sides(dut):
    await _start(dut)
    groups = await _run_grouped(dut, _OHLCV_ROWS, ready_pattern=(1, 0, 0, 1, 0))
    assert groups == grouped_aggregate_rows(_OHLCV_ROWS)


@cocotb.test()
async def grouped_single_group_and_single_row(dut):
    await _start(dut)
    single_group = [(7, 1), (7, 2), (7, 3)]
    assert await _run_grouped(dut, single_group) == grouped_aggregate_rows(single_group)
    single_row = [(9, -42)]
    assert await _run_grouped(dut, single_row) == [GroupAggregate(key=9, first=-42, last=-42, minimum=-42, maximum=-42, total=-42, count=1)]


@cocotb.test()
async def grouped_reappearing_key_emits_partials(dut):
    await _start(dut)
    rows = [(5, 1), (7, 2), (5, 3)]
    groups = await _run_grouped(dut, rows)
    assert [group.key for group in groups] == [5, 7, 5]
    assert groups == grouped_aggregate_rows(rows)


@cocotb.test()
async def grouped_key_change_on_final_row(dut):
    await _start(dut)
    rows = [(1, 10), (1, 20), (2, 30)]
    assert await _run_grouped(dut, rows) == grouped_aggregate_rows(rows)


@cocotb.test()
async def grouped_int64_sum_accumulates_extremes(dut):
    await _start(dut)
    rows = [(3, 2**31 - 1)] * 5 + [(4, -(2**31))] * 5
    groups = await _run_grouped(dut, rows)
    assert groups == grouped_aggregate_rows(rows)
    assert groups[0].total == 5 * (2**31 - 1)
    assert groups[1].total == -5 * (2**31)


@cocotb.test()
async def grouped_repeated_batches_reset_state(dut):
    await _start(dut)
    first = [(1, 5), (2, 6)]
    assert await _run_grouped(dut, first) == grouped_aggregate_rows(first)
    second = [(2, 100)]  # same key as previous batch tail: must be a fresh group
    assert await _run_grouped(dut, second) == [GroupAggregate(key=2, first=100, last=100, minimum=100, maximum=100, total=100, count=1)]


@cocotb.test()
async def grouped_reserved_mode_reports_config_error(dut):
    await _start(dut)
    _apply_config(dut, row_count=2)
    dut.cfg_mode.value = 1  # hash mode not implemented yet
    dut.output_ready.value = 1
    await _send_rows(dut, [(1, 2), (3, 4)])
    await _expect_error(dut, GroupedAggregationErrorCode.CONFIG)


@cocotb.test()
async def grouped_truncated_stream_reports_stream_error(dut):
    await _start(dut)
    _apply_config(dut, row_count=8)
    dut.output_ready.value = 1
    await _send_rows(dut, [(1, 2), (1, 3)])  # input_last after 2 of 8 rows
    await _expect_error(dut, GroupedAggregationErrorCode.STREAM)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    dut.cfg_mode.value = 0
    dut.cfg_row_count.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


def _apply_config(dut, *, row_count: int) -> None:
    dut.cfg_mode.value = 0
    dut.cfg_row_count.value = row_count


async def _run_grouped(dut, rows, *, ready_pattern=(1,)):
    _apply_config(dut, row_count=len(rows))
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_output(dut, ready_pattern)
    await send
    count = await _accept_success_status(dut)
    assert len(words) % 5 == 0, "group records are 5 words"
    assert count == len(words) // 5
    return [_decode_group(words[offset : offset + 5]) for offset in range(0, len(words), 5)]


def _decode_group(words: list[int]) -> GroupAggregate:
    return GroupAggregate(
        key=_signed32(words[0] & 0xFFFFFFFF),
        first=_signed32(words[0] >> 32),
        last=_signed32(words[1] & 0xFFFFFFFF),
        minimum=_signed32(words[1] >> 32),
        maximum=_signed32(words[2] & 0xFFFFFFFF),
        total=_signed64(words[3]),
        count=words[4],
    )


async def _send_rows(dut, rows) -> None:
    for index, (key, value) in enumerate(rows):
        dut.input_data.value = ((value & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF)
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


async def _collect_output(dut, ready_pattern) -> list[int]:
    words: list[int] = []
    pattern_index = 0
    for _ in range(4096):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        pattern_index += 1
        dut.output_ready.value = ready
        await ReadOnly()
        fired = ready == 1 and int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                dut.output_ready.value = 0
                return words
    raise AssertionError("timed out collecting grouped output")


async def _accept_success_status(dut) -> int:
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            count = int(dut.group_count.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return count
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


async def _expect_error(dut, code: GroupedAggregationErrorCode) -> None:
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == int(code)
            assert int(dut.output_valid.value) == 0
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for error status")


def _signed32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


def _signed64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import (
    FieldStats,
    GroupedFieldAggregate,
    bar_aggregate_rows,
    bars_from_field_aggregate,
    grouped_field_aggregate_rows,
)

# (key, v0, v1, v2)
_ROWS = [
    (5, 10, 1, -1),
    (5, 15, 3, 0),
    (5, 8, 2, 4),
    (7, -3, 7, 2),
    (7, -9, 4, 1),
    (9, 100, 0, 0),
]


@cocotb.test()
async def grouped_field_matches_golden(dut):
    await _start(dut)
    groups = await _run(dut, _ROWS)
    assert groups == grouped_field_aggregate_rows(_ROWS)


@cocotb.test()
async def grouped_field_backpressure(dut):
    await _start(dut)
    groups = await _run(dut, _ROWS, ready_pattern=(1, 0, 0, 1, 0))
    assert groups == grouped_field_aggregate_rows(_ROWS)


@cocotb.test()
async def grouped_field_composes_to_bars(dut):
    await _start(dut)
    # bars are a selection over the generic aggregate — the decomposition
    groups = await _run(dut, _ROWS)
    assert bars_from_field_aggregate(groups) == bar_aggregate_rows(_ROWS)


@cocotb.test()
async def grouped_field_reappearing_key_opens_a_new_group(dut):
    await _start(dut)
    rows = [(5, 1, 1, 1), (7, 2, 2, 2), (5, 3, 3, 3)]
    groups = await _run(dut, rows)
    assert [g.key for g in groups] == [5, 7, 5]
    assert groups == grouped_field_aggregate_rows(rows)


@cocotb.test()
async def grouped_field_key_change_on_final_row(dut):
    await _start(dut)
    rows = [(1, 10, 1, 0), (1, 20, 2, 0), (2, 30, 3, 5)]
    assert await _run(dut, rows) == grouped_field_aggregate_rows(rows)


@cocotb.test()
async def grouped_field_int64_sums(dut):
    await _start(dut)
    rows = [(3, 2**31 - 1, 999_999, -(2**31))] * 5
    groups = await _run(dut, rows)
    assert groups == grouped_field_aggregate_rows(rows)
    assert groups[0].fields[0].total == 5 * (2**31 - 1)
    assert groups[0].fields[2].total == 5 * -(2**31)


@cocotb.test()
async def grouped_field_truncated_row_reports_stream_error(dut):
    await _start(dut)
    dut.output_ready.value = 1
    dut.input_data.value = (10 << 32) | 5  # w0 with last: torn row
    dut.input_last.value = 1
    dut.input_valid.value = 1
    while True:
        await ReadOnly()
        fired = int(dut.input_ready.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            break
    dut.input_valid.value = 0
    dut.input_last.value = 0
    await _expect_error(dut)


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
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_output(dut, ready_pattern)
    await send
    count = await _accept_success_status(dut)
    assert len(words) % 11 == 0, "grouped-field records are 11 words"
    assert count == len(words) // 11
    return [_decode(words[offset : offset + 11]) for offset in range(0, len(words), 11)]


def _field(lofihi_lastfirst, minmax, total) -> FieldStats:
    first = _s32(lofihi_lastfirst & 0xFFFFFFFF)
    last = _s32(lofihi_lastfirst >> 32)
    minimum = _s32(minmax & 0xFFFFFFFF)
    maximum = _s32(minmax >> 32)
    return FieldStats(first=first, last=last, minimum=minimum, maximum=maximum, total=_s64(total))


def _decode(w) -> GroupedFieldAggregate:
    return GroupedFieldAggregate(
        key=_s32(w[0] & 0xFFFFFFFF),
        fields=(_field(w[1], w[2], w[3]), _field(w[4], w[5], w[6]), _field(w[7], w[8], w[9])),
        count=w[10],
    )


async def _send_rows(dut, rows) -> None:
    words = []
    for key, v0, v1, v2 in rows:
        words.append(((v0 & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF))
        words.append(((v2 & 0xFFFFFFFF) << 32) | (v1 & 0xFFFFFFFF))
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


async def _collect_output(dut, ready_pattern):
    words = []
    pattern_index = 0
    for _ in range(8192):
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
    raise AssertionError("timed out collecting grouped-field output")


async def _accept_success_status(dut) -> int:
    dut.status_ready.value = 1
    for _ in range(128):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            count = int(dut.group_count.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return count
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


async def _expect_error(dut) -> None:
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == 2
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for error status")


def _s32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


def _s64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value

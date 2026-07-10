from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import FilterErrorCode, FilterPredicate, PredicateCombine, PredicateOp, filter_values

_VALUES = [7, -2, 5, 11, 5, 0, -100, 2**31 - 1, -(2**31)]


@cocotb.test()
async def filter_gt_matches_golden_with_backpressure(dut):
    await _start(dut)
    predicate = FilterPredicate(PredicateOp.GT, 4)
    kept, count = await _run_filter(dut, _VALUES, predicate, ready_pattern=(1, 0, 0, 1, 1, 0))
    golden = filter_values(_VALUES, predicate)
    assert kept == golden
    assert count == len(golden)


@cocotb.test()
async def filter_and_is_range(dut):
    await _start(dut)
    predicate = FilterPredicate(PredicateOp.GE, 0, PredicateCombine.AND, PredicateOp.LT, 10)
    kept, count = await _run_filter(dut, _VALUES, predicate)
    golden = filter_values(_VALUES, predicate)
    assert kept == golden
    assert count == len(golden)


@cocotb.test()
async def filter_or_is_union(dut):
    await _start(dut)
    predicate = FilterPredicate(PredicateOp.LT, -50, PredicateCombine.OR, PredicateOp.GT, 50)
    kept, count = await _run_filter(dut, _VALUES, predicate)
    golden = filter_values(_VALUES, predicate)
    assert kept == golden
    assert count == len(golden)


@cocotb.test()
async def filter_empty_selection_emits_no_output(dut):
    await _start(dut)
    predicate = FilterPredicate(PredicateOp.GT, 2**31 - 1)
    kept, count = await _run_filter(dut, _VALUES, predicate, expect_output=False)
    assert kept == []
    assert count == 0


@cocotb.test()
async def filter_all_match_passes_through(dut):
    await _start(dut)
    values = [1, 2, 3, 4, 5, 6]
    predicate = FilterPredicate(PredicateOp.NE, 99)
    kept, count = await _run_filter(dut, values, predicate)
    assert kept == values
    assert count == len(values)


@cocotb.test()
async def filter_repeated_batches_relatch_config(dut):
    await _start(dut)
    first = FilterPredicate(PredicateOp.LT, 0)
    kept, _ = await _run_filter(dut, _VALUES, first)
    assert kept == filter_values(_VALUES, first)

    second = FilterPredicate(PredicateOp.EQ, 5)
    kept, count = await _run_filter(dut, _VALUES, second)
    assert kept == filter_values(_VALUES, second)
    assert count == 2


@cocotb.test()
async def filter_invalid_op_reports_config_error(dut):
    await _start(dut)
    _apply_config(dut, FilterPredicate(PredicateOp.EQ, 0), row_count=4)
    dut.cfg_op0.value = 0  # reserved opcode
    await _send_values(dut, [1, 2, 3, 4])
    await _expect_error(dut, FilterErrorCode.CONFIG)


@cocotb.test()
async def filter_truncated_stream_reports_stream_error(dut):
    await _start(dut)
    _apply_config(dut, FilterPredicate(PredicateOp.NE, 0), row_count=8)
    await _send_values(dut, [1, 2, 3, 4])  # input_last after 4 of 8 rows
    await _expect_error(dut, FilterErrorCode.STREAM)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    for name in ("cfg_op0", "cfg_constant0", "cfg_combine", "cfg_op1", "cfg_constant1", "cfg_row_count"):
        getattr(dut, name).value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


def _apply_config(dut, predicate: FilterPredicate, *, row_count: int) -> None:
    dut.cfg_op0.value = int(predicate.op0)
    dut.cfg_constant0.value = predicate.constant0 & 0xFFFFFFFF
    dut.cfg_combine.value = int(predicate.combine)
    dut.cfg_op1.value = int(predicate.op1)
    dut.cfg_constant1.value = predicate.constant1 & 0xFFFFFFFF
    dut.cfg_row_count.value = row_count


async def _run_filter(dut, values, predicate, *, ready_pattern=(1,), expect_output=True):
    _apply_config(dut, predicate, row_count=len(values))
    send = cocotb.start_soon(_send_values(dut, values))
    samples = await _collect_output(dut, ready_pattern) if expect_output else []
    await send
    count = await _accept_success_status(dut)
    assert len(samples) in (count, count + 1), "output word count inconsistent with selected_count"
    return samples[:count], count


async def _send_values(dut, values) -> None:
    words = []
    for offset in range(0, len(values), 2):
        low = values[offset] & 0xFFFFFFFF
        high = (values[offset + 1] & 0xFFFFFFFF) if offset + 1 < len(values) else 0
        words.append((high << 32) | low)
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


async def _collect_output(dut, ready_pattern) -> list[int]:
    samples: list[int] = []
    pattern_index = 0
    for _ in range(512):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        pattern_index += 1
        dut.output_ready.value = ready
        await ReadOnly()
        fired = ready == 1 and int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        await RisingEdge(dut.clk)
        if fired:
            samples.append(_signed(word & 0xFFFFFFFF))
            samples.append(_signed(word >> 32))
            if last == 1:
                dut.output_ready.value = 0
                return samples
    raise AssertionError("timed out collecting filter output")


async def _accept_success_status(dut) -> int:
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            count = int(dut.selected_count.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return count
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


async def _expect_error(dut, code: FilterErrorCode) -> None:
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


def _signed(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value

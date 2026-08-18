"""Wide-lane row predicate filter, golden-equal to ``filter_rows_by_predicate``."""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import (
    FilterErrorCode,
    PredicateCombine,
    PredicateOp,
    RowField,
    RowPredicateConfig,
    RowPredicateTerm,
    filter_rows_by_predicate,
)
from dau_core.tests.cocotb_benches.dau_int32_wide_field_sum_aggregation_tb import (
    _PAD_ROW,
    _beats,
    _row_bits,
    _send_beats,
    _slots,
)
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

_MASK32 = 0xFFFF_FFFF


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    slots = _slots(dut)
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.GE, constant=0))
    _drive_config(dut, config)
    rows = [(index, index + 1, index + 2, index + 3) for index in range(256 * slots)]
    beats = _beats(rows, slots)
    await assert_sustained_ingest(
        dut,
        "wide-row-predicate-filter",
        send=lambda: _send_beats(dut, beats),
        input_beats=len(beats),
        rows_per_beat=slots,
        params={"SLOTS": slots},
    )


@cocotb.test()
async def random_ragged_rows_match_golden_under_backpressure(dut):
    await _start(dut)
    rng = random.Random(0xF117_E4)
    rows = [tuple(rng.randint(-(1 << 31), (1 << 31) - 1) for _ in range(4)) for _ in range(4 * _slots(dut) + 1)]
    config = RowPredicateConfig(
        term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.GE, constant=0),
        combine01=PredicateCombine.OR,
        term1=RowPredicateTerm(field=RowField.VALUE, op=PredicateOp.LT, constant=0),
        combine2=PredicateCombine.AND,
        term2=RowPredicateTerm(field=RowField.AUX1, op=PredicateOp.NE, constant=0),
    )

    _, decoded = await _run(dut, rows, config, ready_pattern=(1, 0, 0))

    expected = filter_rows_by_predicate(rows, config)
    assert decoded == expected
    assert int(dut.selected_count.value) == len(expected)


@cocotb.test()
async def pads_pass_untouched_and_are_not_evaluated(dut):
    await _start(dut)
    slots = _slots(dut)
    rows = [(7, 8, 9, 10)]
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.NE, constant=0))
    input_beats = _beats(rows, slots) + [_all_pad_beat(slots)]

    output_beats, decoded = await _run(dut, rows, config, input_beats=input_beats)

    assert output_beats[-1] == _all_pad_beat(slots)
    assert decoded == rows
    assert int(dut.selected_count.value) == 1


@cocotb.test()
async def minus_one_predicate_fields_in_a_real_row_are_evaluated(dut):
    await _start(dut)
    rows = [(-1, -1, -1, 7), (-1, -1, 0, 7)]
    config = RowPredicateConfig(
        term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=-1),
        combine01=PredicateCombine.AND,
        term1=RowPredicateTerm(field=RowField.VALUE, op=PredicateOp.EQ, constant=-1),
        combine2=PredicateCombine.AND,
        term2=RowPredicateTerm(field=RowField.AUX0, op=PredicateOp.EQ, constant=-1),
    )

    _, decoded = await _run(dut, rows, config)

    assert decoded == filter_rows_by_predicate(rows, config) == [rows[0]]
    assert int(dut.selected_count.value) == 1


@cocotb.test()
async def invalid_config_drains_and_reports_error(dut):
    await _start(dut)
    rows = [(index, index + 1, index + 2, index + 3) for index in range(2 * _slots(dut) + 1)]
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=0))
    _drive_config(dut, config)
    dut.cfg_op0.value = 0
    dut.output_ready.value = 1

    await _send_beats(dut, _beats(rows, _slots(dut)))
    await _expect_error(dut, FilterErrorCode.CONFIG)

    assert int(dut.output_valid.value) == 0
    assert int(dut.selected_count.value) == 0


@cocotb.test()
async def back_to_back_batches_relatch_config(dut):
    await _start(dut)
    rows_a = [(-3, 10, 1, 2), (4, 20, 3, 4), (8, 30, 5, 6)]
    rows_b = [(1, 100, 7, 8), (2, -100, 9, 10), (3, 0, 11, 12)]
    config_a = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.GT, constant=0))
    config_b = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.VALUE, op=PredicateOp.LE, constant=0))

    _, decoded_a = await _run(dut, rows_a, config_a)
    _, decoded_b = await _run(dut, rows_b, config_b)

    assert decoded_a == filter_rows_by_predicate(rows_a, config_a)
    assert decoded_b == filter_rows_by_predicate(rows_b, config_b)
    assert int(dut.selected_count.value) == len(decoded_b)


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _decode_row(bits: int) -> tuple[int, int, int, int]:
    return tuple(_signed32(bits >> (32 * field)) for field in range(4))


def _decode_non_pad_rows(beats, slots: int) -> list[tuple[int, int, int, int]]:
    rows = []
    for beat in beats:
        for slot in range(slots):
            bits = (beat >> (128 * slot)) & _PAD_ROW
            if bits != _PAD_ROW:
                rows.append(_decode_row(bits))
    return rows


def _expected_beats(input_beats, slots: int, config: RowPredicateConfig) -> list[int]:
    expected = []
    for beat in input_beats:
        filtered = beat
        for slot in range(slots):
            shift = 128 * slot
            bits = (beat >> shift) & _PAD_ROW
            if bits != _PAD_ROW:
                row = _decode_row(bits)
                replacement = _row_bits(row) if config.matches(row) else _PAD_ROW
                filtered = (filtered & ~(_PAD_ROW << shift)) | (replacement << shift)
        expected.append(filtered)
    return expected


def _all_pad_beat(slots: int) -> int:
    return sum(_PAD_ROW << (128 * slot) for slot in range(slots))


def _drive_config(dut, config: RowPredicateConfig) -> None:
    term1 = config.term1 or RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=0)
    term2 = config.term2 or RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=0)
    dut.cfg_field0.value = int(config.term0.field)
    dut.cfg_op0.value = int(config.term0.op)
    dut.cfg_constant0.value = config.term0.constant & _MASK32
    dut.cfg_combine01.value = int(config.combine01)
    dut.cfg_field1.value = int(term1.field)
    dut.cfg_op1.value = int(term1.op)
    dut.cfg_constant1.value = term1.constant & _MASK32
    dut.cfg_combine2.value = int(config.combine2)
    dut.cfg_field2.value = int(term2.field)
    dut.cfg_op2.value = int(term2.op)
    dut.cfg_constant2.value = term2.constant & _MASK32


async def _start(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start(start_high=False))
    dut.rst.value = 1
    _drive_config(dut, RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=0)))
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, config: RowPredicateConfig, *, ready_pattern=(1,), input_beats=None):
    slots = _slots(dut)
    driven_beats = list(input_beats) if input_beats is not None else _beats(rows, slots)
    _drive_config(dut, config)
    sender = cocotb.start_soon(_send_beats(dut, driven_beats))
    output_beats = await _collect_output(dut, ready_pattern, expect_beats=len(driven_beats))
    await sender

    assert output_beats == _expected_beats(driven_beats, slots, config)
    decoded = _decode_non_pad_rows(output_beats, slots)
    assert decoded == filter_rows_by_predicate(rows, config)
    return output_beats, decoded


async def _collect_output(dut, ready_pattern, *, expect_beats: int) -> list[int]:
    beats = []
    held = None
    for pattern_index in range(200000):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        dut.output_ready.value = ready
        await ReadOnly()
        valid = int(dut.output_valid.value) == 1
        beat = int(dut.output_data.value)
        last = int(dut.output_last.value)
        fired = ready == 1 and valid
        if held is not None:
            assert valid, "output_valid dropped while a beat was stalled"
            assert (beat, last) == held, "output beat changed while stalled"
        held = None if (fired or not valid) else (beat, last)
        await RisingEdge(dut.clk)
        if fired:
            beats.append(beat)
            assert last == int(len(beats) == expect_beats), "output_last did not mark exactly the final beat"
            if last:
                assert len(beats) == expect_beats
                await _assert_quiescent(dut)
                return beats
    raise AssertionError(f"timed out collecting filter output ({len(beats)}/{expect_beats} beats)")


async def _assert_quiescent(dut) -> None:
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    for _ in range(6):
        await ReadOnly()
        assert int(dut.output_valid.value) == 0, "output beat after batch completion"
        assert int(dut.status_valid.value) == 0, "status close-out after successful batch"
        await RisingEdge(dut.clk)
    dut.output_ready.value = 0
    dut.status_ready.value = 0


async def _expect_error(dut, code: int) -> None:
    dut.status_ready.value = 0
    found = False
    for _ in range(32):
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        if found:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == int(code)
        await RisingEdge(dut.clk)
        if found:
            break
    assert found, "error status never asserted"
    for _ in range(3):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1
        assert int(dut.status_error.value) == 1
        assert int(dut.status_error_code.value) == int(code)
        await RisingEdge(dut.clk)
    dut.status_ready.value = 1
    await ReadOnly()
    fired = int(dut.status_valid.value) == 1
    await RisingEdge(dut.clk)
    assert fired, "error status did not handshake"
    dut.status_ready.value = 0

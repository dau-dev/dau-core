"""Wide-lane row map/ALU, golden-equal to ``map_row_fields``."""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import MapErrorCode, MapOp, RowField, RowMapConfig, map_row_fields
from dau_core.tests.cocotb_benches.dau_int32_wide_field_sum_aggregation_tb import (
    _PAD_ROW,
    _beats,
    _row_bits,
    _send_beats,
    _slots,
)

_MASK32 = 0xFFFF_FFFF


@cocotb.test()
async def random_ragged_rows_match_golden_under_backpressure(dut):
    await _start(dut)
    rng = random.Random(0xA111_2026)
    rows = [tuple(rng.randint(-(1 << 31), (1 << 31) - 1) for _ in range(4)) for _ in range(4 * _slots(dut) + 1)]
    config = RowMapConfig(
        op=MapOp.MULSHR,
        dst=RowField.AUX0,
        src_a=RowField.VALUE,
        src_b=RowField.AUX1,
        shift=7,
    )

    _, decoded = await _run(dut, rows, config, ready_pattern=(1, 0, 0))

    assert decoded == map_row_fields(rows, config)


@cocotb.test()
async def pads_pass_untouched_and_are_not_transformed(dut):
    await _start(dut)
    slots = _slots(dut)
    rows = [(7, 8, 9, 10)]
    config = RowMapConfig(op=MapOp.ADD, dst=RowField.KEY, src_a=RowField.KEY, src_b=RowField.VALUE)
    input_beats = _beats(rows, slots) + [_all_pad_beat(slots)]

    output_beats, decoded = await _run(dut, rows, config, input_beats=input_beats)

    assert output_beats[-1] == _all_pad_beat(slots)
    assert decoded == map_row_fields(rows, config)


@cocotb.test()
async def minus_one_source_fields_in_a_real_row_are_transformed(dut):
    await _start(dut)
    rows = [(-1, -1, -1, 7)]
    config = RowMapConfig(op=MapOp.ADD, dst=RowField.AUX1, src_a=RowField.KEY, src_b=RowField.VALUE)

    _, decoded = await _run(dut, rows, config)

    assert decoded == map_row_fields(rows, config) == [(-1, -1, -1, -2)]


@cocotb.test()
async def invalid_config_drains_and_reports_error(dut):
    await _start(dut)
    rows = [(index, index + 1, index + 2, index + 3) for index in range(2 * _slots(dut) + 1)]
    config = RowMapConfig(op=MapOp.ADD, dst=RowField.KEY, src_a=RowField.KEY, src_b=RowField.VALUE)
    _drive_config(dut, config)
    dut.cfg_op.value = 0
    dut.output_ready.value = 1

    await _send_beats(dut, _beats(rows, _slots(dut)))
    await _expect_error(dut, MapErrorCode.CONFIG)

    assert int(dut.output_valid.value) == 0


@cocotb.test()
async def back_to_back_batches_relatch_config(dut):
    await _start(dut)
    rows_a = [(1, 10, 100, 1000), (2, 20, 200, 2000), (3, 30, 300, 3000)]
    rows_b = [(-4, 7, 9, 11), (5, -8, 10, 12)]
    config_a = RowMapConfig(op=MapOp.ADD, dst=RowField.AUX0, src_a=RowField.KEY, src_b=RowField.VALUE)
    config_b = RowMapConfig(op=MapOp.SUB, dst=RowField.AUX1, src_a=RowField.VALUE, src_b=RowField.KEY)

    _, decoded_a = await _run(dut, rows_a, config_a)
    _, decoded_b = await _run(dut, rows_b, config_b)

    assert decoded_a == map_row_fields(rows_a, config_a)
    assert decoded_b == map_row_fields(rows_b, config_b)


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


def _expected_beats(input_beats, slots: int, config: RowMapConfig) -> list[int]:
    expected = []
    for beat in input_beats:
        mapped_beat = beat
        for slot in range(slots):
            shift = 128 * slot
            bits = (beat >> shift) & _PAD_ROW
            if bits != _PAD_ROW:
                mapped = map_row_fields([_decode_row(bits)], config)[0]
                mapped_beat = (mapped_beat & ~(_PAD_ROW << shift)) | (_row_bits(mapped) << shift)
        expected.append(mapped_beat)
    return expected


def _all_pad_beat(slots: int) -> int:
    return sum(_PAD_ROW << (128 * slot) for slot in range(slots))


def _drive_config(dut, config: RowMapConfig) -> None:
    dut.cfg_op.value = int(config.op)
    dut.cfg_dst_field.value = int(config.dst)
    dut.cfg_src_a_field.value = int(config.src_a)
    dut.cfg_src_b_field.value = int(config.src_b)
    dut.cfg_shift.value = config.shift


async def _start(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start(start_high=False))
    dut.rst.value = 1
    _drive_config(dut, RowMapConfig(op=MapOp.ADD, dst=RowField.KEY, src_a=RowField.KEY, src_b=RowField.KEY))
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, config: RowMapConfig, *, ready_pattern=(1,), input_beats=None):
    slots = _slots(dut)
    driven_beats = list(input_beats) if input_beats is not None else _beats(rows, slots)
    _drive_config(dut, config)
    sender = cocotb.start_soon(_send_beats(dut, driven_beats))
    output_beats = await _collect_output(dut, ready_pattern, expect_beats=len(driven_beats))
    await sender

    assert output_beats == _expected_beats(driven_beats, slots, config)
    decoded = _decode_non_pad_rows(output_beats, slots)
    assert decoded == map_row_fields(rows, config)
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
    raise AssertionError(f"timed out collecting map output ({len(beats)}/{expect_beats} beats)")


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

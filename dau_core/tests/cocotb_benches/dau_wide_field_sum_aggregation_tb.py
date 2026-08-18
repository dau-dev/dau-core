"""Wide-lane field-sum terminal: SLOTS whole quad rows per beat, one beat
per cycle, golden-equal to ``field_sum_rows``. The all-ones 128-bit PAD
row (ragged tails, upstream drop-by-pad) is neither summed nor counted;
the record/close-out protocol is byte-identical to the 64-bit tile. The
width-agnostic collect/status harness is shared with the 64-bit bench."""

from __future__ import annotations

import random

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import field_sum_rows
from dau_core.tests.cocotb_benches.dau_field_sum_aggregation_tb import (
    _MASK64,
    _collect_output,
    _expect_status,
    _start,
)
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

_PAD_ROW = (1 << 128) - 1


def _slots(dut) -> int:
    return len(dut.input_data) // 128


def _row_bits(row) -> int:
    key, value, aux0, aux1 = row
    mask = 0xFFFF_FFFF
    return (key & mask) | ((value & mask) << 32) | ((aux0 & mask) << 64) | ((aux1 & mask) << 96)


def _beats(rows, slots: int) -> list[int]:
    beats = []
    for start in range(0, len(rows), slots):
        chunk = rows[start : start + slots]
        beat = 0
        for i in range(slots):
            bits = _row_bits(chunk[i]) if i < len(chunk) else _PAD_ROW
            beat |= bits << (128 * i)
        beats.append(beat)
    return beats


async def _send_beats(dut, beats) -> None:
    for index, beat in enumerate(beats):
        dut.input_data.value = beat
        dut.input_last.value = int(index == len(beats) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _run(dut, rows, *, field_mask: int, ready_pattern=(1,), beats=None):
    dut.cfg_field_mask.value = field_mask
    expect_words = field_mask.bit_count() + 1
    send = cocotb.start_soon(_send_beats(dut, beats if beats is not None else _beats(rows, _slots(dut))))
    words = await _collect_output(dut, ready_pattern, expect_words=expect_words)
    await send
    return words


def _random_rows(rng: random.Random, count: int):
    return [tuple(rng.randint(-(1 << 31), (1 << 31) - 1) for _ in range(4)) for _ in range(count)]


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    slots = _slots(dut)
    dut.cfg_field_mask.value = 0b0010
    rows = [(index, index + 1, index + 2, index + 3) for index in range(256 * slots)]
    beats = _beats(rows, slots)
    await assert_sustained_ingest(
        dut,
        "wide-field-sum-aggregation",
        send=lambda: _send_beats(dut, beats),
        input_beats=len(beats),
        rows_per_beat=slots,
        params={"SLOTS": slots},
    )


@cocotb.test()
async def wide_sums_match_the_64_bit_golden(dut):
    await _start(dut)
    rng = random.Random(20260731)
    rows = _random_rows(rng, 4 * _slots(dut) + 1)  # ragged tail pads the last beat
    for mask in (0b0010, 0b1111, 0b1001):
        record = await _run(dut, rows, field_mask=mask)
        assert record == field_sum_rows(rows, field_mask=mask).record_words()


@cocotb.test()
async def pad_rows_are_neither_summed_nor_counted(dut):
    # every ragged tail exercises pads; this pins it explicitly with a
    # single real row in a full-width beat of pads
    await _start(dut)
    rows = [(1, 7, 3, 4)]
    record = await _run(dut, rows, field_mask=0b1111)
    assert record == field_sum_rows(rows, field_mask=0b1111).record_words()
    assert record[-1] == 1


@cocotb.test()
async def all_ones_fields_still_sum_when_the_row_is_not_pad(dut):
    # only the FULL all-ones row is the pad: -1 field values in a real row
    # accumulate normally
    await _start(dut)
    rows = [(-1, -1, -1, 2), (3, 4, 5, 6)]
    record = await _run(dut, rows, field_mask=0b1111)
    assert record == field_sum_rows(rows, field_mask=0b1111).record_words()


@cocotb.test()
async def a_trailing_all_pad_beat_closes_the_batch(dut):
    # defensive framing (the wide-dispatcher precedent): a full final beat
    # followed by an all-pad last beat still closes with the right record
    await _start(dut)
    slots = _slots(dut)
    rows = [(i, i + 10, i + 20, i + 30) for i in range(slots)]
    beats = _beats(rows, slots)
    all_pad = 0
    for i in range(slots):
        all_pad |= _PAD_ROW << (128 * i)
    beats.append(all_pad)
    record = await _run(dut, rows, field_mask=0b0010, beats=beats)
    assert record == field_sum_rows(rows, field_mask=0b0010).record_words()


@cocotb.test()
async def backpressured_record_drain_is_stable(dut):
    await _start(dut)
    rng = random.Random(20260801)
    rows = _random_rows(rng, 6 * _slots(dut))
    record = await _run(dut, rows, field_mask=0b1111, ready_pattern=(1, 0, 0))
    assert record == field_sum_rows(rows, field_mask=0b1111).record_words()


@cocotb.test()
async def zero_mask_drains_and_reports_config_error(dut):
    await _start(dut)
    rows = [(1, 2, 3, 4), (5, 6, 7, 8)]
    dut.cfg_field_mask.value = 0
    send = cocotb.start_soon(_send_beats(dut, _beats(rows, _slots(dut))))
    await send
    await _expect_status(dut, error=True, code=1)  # ERR_CONFIG
    assert int(dut.aggregated_count.value) == 0


@cocotb.test()
async def back_to_back_batches_relatch_the_mask(dut):
    await _start(dut)
    rows_a = [(1, 100, 2, 3), (4, 200, 5, 6)]
    rows_b = [(7, 300, 8, 9)]
    record_a = await _run(dut, rows_a, field_mask=0b0010)
    assert record_a == field_sum_rows(rows_a, field_mask=0b0010).record_words()
    record_b = await _run(dut, rows_b, field_mask=0b1100)
    assert record_b == field_sum_rows(rows_b, field_mask=0b1100).record_words()
    assert record_a[0] == (100 + 200) & _MASK64

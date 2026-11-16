"""Wide group-table sum: SLOTS whole quad rows per beat into a direct-indexed
table of per-group sums, golden-equal to ``group_table_sum_rows``. Pads are
neither summed nor counted; a group's rows merge wherever they fall in the
batch, including in the same beat and in consecutive beats; the table is
empty again for the next batch."""

from __future__ import annotations

import random

import cocotb

from dau_core.operators import group_table_sum_rows
from dau_core.tests.cocotb_benches.dau_field_sum_aggregation_tb import _collect_output, _expect_status, _start
from dau_core.tests.cocotb_benches.dau_wide_field_sum_aggregation_tb import _beats, _send_beats, _slots
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

_GROUP_BITS = 6


def _golden_words(rows, *, field_mask: int, group_shift: int) -> list[int]:
    return [
        word
        for group in group_table_sum_rows(rows, field_mask=field_mask, group_shift=group_shift, group_bits=_GROUP_BITS)
        for word in group.record_words()
    ]


async def _run(dut, rows, *, field_mask: int, group_shift: int = 0, ready_pattern=(1,)) -> list[int]:
    dut.cfg_field_mask.value = field_mask
    dut.cfg_group_shift.value = group_shift
    expected = _golden_words(rows, field_mask=field_mask, group_shift=group_shift)
    send = cocotb.start_soon(_send_beats(dut, _beats(rows, _slots(dut))))
    if not expected:
        await send
        await _expect_status(dut, error=False)
        return []
    words = await _collect_output(dut, ready_pattern, expect_words=len(expected))
    await send
    return words


def _rows(rng: random.Random, count: int, *, groups: int, shift: int = 0) -> list[tuple[int, int, int, int]]:
    rows = []
    for _ in range(count):
        low = rng.randrange(1 << shift) if shift else 0
        key = (rng.randrange(groups) << shift) | low | (rng.randrange(1 << 8) << (shift + _GROUP_BITS))
        value, aux0, aux1 = (rng.randint(-(1 << 31), (1 << 31) - 1) for _ in range(3))
        rows.append((key - (1 << 32) if key >= 1 << 31 else key, value, aux0, aux1))
    return rows


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    slots = _slots(dut)
    dut.cfg_field_mask.value = 0b1110
    dut.cfg_group_shift.value = 0
    rows = [(index % 44, index, -index, 3) for index in range(256 * slots)]
    beats = _beats(rows, slots)
    await assert_sustained_ingest(
        dut, "wide-group-table-sum", send=lambda: _send_beats(dut, beats), input_beats=len(beats), rows_per_beat=slots, params={"SLOTS": slots}
    )


@cocotb.test()
async def groups_merge_wherever_their_rows_fall(dut):
    await _start(dut)
    rng = random.Random(20261115)
    rows = _rows(rng, 9 * _slots(dut) + 3, groups=44)  # ragged tail pads the last beat
    for mask in (0b1110, 0b0010, 0b1111):
        assert await _run(dut, rows, field_mask=mask) == _golden_words(rows, field_mask=mask, group_shift=0)


@cocotb.test()
async def one_group_in_every_slot_of_consecutive_beats(dut):
    await _start(dut)
    rows = [(7, index, 2 * index, -index) for index in range(6 * _slots(dut))]
    assert await _run(dut, rows, field_mask=0b1110) == _golden_words(rows, field_mask=0b1110, group_shift=0)


@cocotb.test()
async def the_group_code_comes_from_the_shifted_key(dut):
    await _start(dut)
    rng = random.Random(7)
    rows = _rows(rng, 5 * _slots(dut), groups=64, shift=9)
    assert await _run(dut, rows, field_mask=0b1010, group_shift=9) == _golden_words(rows, field_mask=0b1010, group_shift=9)


@cocotb.test()
async def the_table_is_empty_for_the_next_batch(dut):
    await _start(dut)
    rng = random.Random(11)
    first = _rows(rng, 3 * _slots(dut), groups=10)
    second = _rows(rng, 2 * _slots(dut), groups=10)
    await _run(dut, first, field_mask=0b0110)
    assert await _run(dut, second, field_mask=0b0110) == _golden_words(second, field_mask=0b0110, group_shift=0)


@cocotb.test()
async def backpressured_records_drain_stably(dut):
    await _start(dut)
    rng = random.Random(3)
    rows = _rows(rng, 4 * _slots(dut), groups=12)
    assert await _run(dut, rows, field_mask=0b1110, ready_pattern=(1, 0, 0, 1, 0)) == _golden_words(rows, field_mask=0b1110, group_shift=0)


@cocotb.test()
async def an_all_pad_batch_emits_no_record(dut):
    await _start(dut)
    dut.cfg_field_mask.value = 0b0010
    dut.cfg_group_shift.value = 0
    await _send_beats(dut, [(1 << (128 * _slots(dut))) - 1])
    await _expect_status(dut, error=False)
    rng = random.Random(5)
    rows = _rows(rng, 2 * _slots(dut), groups=8)
    assert await _run(dut, rows, field_mask=0b0010) == _golden_words(rows, field_mask=0b0010, group_shift=0)


@cocotb.test()
async def zero_mask_drains_and_reports_config_error(dut):
    await _start(dut)
    rows = [(1, 2, 3, 4)] * (3 * _slots(dut))
    dut.cfg_field_mask.value = 0
    dut.cfg_group_shift.value = 0
    send = cocotb.start_soon(_send_beats(dut, _beats(rows, _slots(dut))))
    await _expect_status(dut, error=True, code=1)
    await send
    assert await _run(dut, rows, field_mask=0b0010) == _golden_words(rows, field_mask=0b0010, group_shift=0)

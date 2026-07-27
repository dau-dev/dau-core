from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import GroupedFieldSumErrorCode, grouped_field_sum_rows

_MASK32 = 0xFFFF_FFFF
_MASK64 = 0xFFFF_FFFF_FFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# (key, value, aux0, aux1) pre-sorted on the key — the q1 field map for the
# charge pass is (packed returnflag/linestatus, disc_price, scratch, charge)
_ROWS = [
    (3, 1000, 5, 10),
    (3, 2000, 6, 30),
    (3, -1500, 7, 23),
    (7, -500, 6, 24),
    (9, 3000, 4, 1),
    (9, 1, 2, 3),
]

_Q1_MASK = 0b1010  # SUM(disc_price) + SUM(charge) per group


def _record_words(rows, field_mask: int) -> list[int]:
    words: list[int] = []
    for group in grouped_field_sum_rows(rows, field_mask=field_mask):
        words.extend(group.record_words())
    return words


@cocotb.test()
async def q1_mask_emits_one_record_per_group(dut):
    await _start(dut)
    words = await _run(dut, _ROWS, field_mask=_Q1_MASK)
    assert words == _record_words(_ROWS, _Q1_MASK)
    # three groups x (key + two sums + count)
    assert len(words) == 3 * 4


@cocotb.test()
async def full_mask_walks_every_field(dut):
    await _start(dut)
    words = await _run(dut, _ROWS, field_mask=0b1111)
    assert words == _record_words(_ROWS, 0b1111)
    assert len(words) == 3 * 6


@cocotb.test()
async def sparse_masks_emit_ascending_field_order(dut):
    await _start(dut)
    for mask in (0b0001, 0b0010, 0b0100, 0b1000, 0b0101, 0b1010, 0b1001, 0b0111):
        words = await _run(dut, _ROWS, field_mask=mask)
        assert words == _record_words(_ROWS, mask), f"mask {mask:#06b}"


@cocotb.test()
async def single_group_batch_closes_at_input_end(dut):
    await _start(dut)
    rows = [(42, 1, 2, 3), (42, 4, 5, 6), (42, 7, 8, 9)]
    words = await _run(dut, rows, field_mask=0b1110)
    assert words == _record_words(rows, 0b1110)
    assert int(dut.group_count.value) == 1


@cocotb.test()
async def every_row_its_own_group(dut):
    await _start(dut)
    rows = [(k, k * 10, k * 100, k * 1000) for k in range(1, 7)]
    words = await _run(dut, rows, field_mask=0b0010)
    assert words == _record_words(rows, 0b0010)
    assert int(dut.group_count.value) == len(rows)


@cocotb.test()
async def zero_valued_fields_sum_to_zero_per_group(dut):
    # every selected field is zero in every group: sums are all zero but
    # the group records (key + count) still emit
    await _start(dut)
    rows = [(1, 0, 5, 0), (1, 0, 6, 0), (2, 0, 7, 0)]
    words = await _run(dut, rows, field_mask=0b1010)
    assert words == _record_words(rows, 0b1010)
    assert words == [1, 0, 0, 2, 2, 0, 0, 1]


@cocotb.test()
async def reappearing_key_opens_a_new_group(dut):
    await _start(dut)
    rows = [(5, 10, 0, 0), (6, 20, 0, 0), (5, 30, 0, 0)]
    words = await _run(dut, rows, field_mask=0b0010)
    golden = grouped_field_sum_rows(rows, field_mask=0b0010)
    assert [group.key for group in golden] == [5, 6, 5]
    assert words == _record_words(rows, 0b0010)
    assert int(dut.group_count.value) == 3


@cocotb.test()
async def negative_keys_and_sums_are_twos_complement(dut):
    await _start(dut)
    rows = [(-3, -1000, 0, 0), (-3, -2000, 0, 0), (2, -5, 0, 0)]
    words = await _run(dut, rows, field_mask=0b0010)
    assert words == [(-3) & _MASK64, (-3000) & _MASK64, 2, 2, (-5) & _MASK64, 1]


@cocotb.test()
async def int32_extreme_sums_accumulate_int64(dut):
    await _start(dut)
    rows = [(_INT32_MIN, _INT32_MAX, _INT32_MIN, _INT32_MAX)] * 6 + [(7, 1, 1, 1)]
    words = await _run(dut, rows, field_mask=0b1111)
    golden = grouped_field_sum_rows(rows, field_mask=0b1111)
    assert words == _record_words(rows, 0b1111)
    assert golden[0].sums[0] == 6 * _INT32_MIN
    assert golden[0].sums[1] == 6 * _INT32_MAX


@cocotb.test()
async def group_count_reports_groups_of_last_batch(dut):
    await _start(dut)
    await _run(dut, _ROWS, field_mask=_Q1_MASK)
    assert int(dut.group_count.value) == 3
    await _run(dut, [(1, 2, 3, 4)], field_mask=_Q1_MASK)
    assert int(dut.group_count.value) == 1


@cocotb.test()
async def repeated_batches_relatch_the_mask(dut):
    await _start(dut)
    first = await _run(dut, _ROWS, field_mask=0b0010)
    second = await _run(dut, _ROWS, field_mask=0b1101)
    assert first == _record_words(_ROWS, 0b0010)
    assert second == _record_words(_ROWS, 0b1101)


@cocotb.test()
async def backpressure_on_the_record_drain(dut):
    await _start(dut)
    words = await _run(dut, _ROWS, field_mask=0b1111, ready_pattern=(1, 0, 0, 1, 0))
    assert words == _record_words(_ROWS, 0b1111)


@cocotb.test()
async def group_boundary_under_backpressure_holds_input(dut):
    # a key change forces a mid-batch record drain; with the drain stalled
    # the tile must not lose the held row that opens the next group
    await _start(dut)
    rows = [(1, 100, 0, 0), (2, 200, 0, 0), (3, 300, 0, 0)]
    words = await _run(dut, rows, field_mask=0b0010, ready_pattern=(0, 0, 0, 1))
    assert words == _record_words(rows, 0b0010)


@cocotb.test()
async def zero_mask_drains_batch_then_reports_config_error(dut):
    await _start(dut)
    dut.cfg_field_mask.value = 0  # unconfigured tile fails explicitly
    send = cocotb.start_soon(_send_rows(dut, _ROWS))
    await _expect_status(dut, error=True, code=GroupedFieldSumErrorCode.CONFIG)
    await send
    assert int(dut.group_count.value) == 0

    words = await _run(dut, _ROWS, field_mask=_Q1_MASK)
    assert words == _record_words(_ROWS, _Q1_MASK)


@cocotb.test()
async def torn_row_reports_stream_error(dut):
    await _start(dut)
    dut.cfg_field_mask.value = _Q1_MASK
    # input_last on a w0: a torn row
    await _send_words(dut, [(100 << 32) | 5])
    await _expect_status(dut, error=True, code=GroupedFieldSumErrorCode.STREAM)
    assert int(dut.group_count.value) == 0

    words = await _run(dut, _ROWS, field_mask=_Q1_MASK)
    assert words == _record_words(_ROWS, _Q1_MASK)


@cocotb.test()
async def torn_row_on_a_group_boundary_reports_stream_error(dut):
    # the torn w0 carries a NEW key: the group close-out path must still
    # resolve to a stream error, not a half-emitted record
    await _start(dut)
    dut.cfg_field_mask.value = _Q1_MASK
    send = cocotb.start_soon(_send_words(dut, _pack_rows([(1, 10, 0, 0), (1, 20, 0, 0)])[:4] + [(999 << 32) | 2]))
    await _expect_status(dut, error=True, code=GroupedFieldSumErrorCode.STREAM)
    await send
    assert int(dut.group_count.value) == 0

    words = await _run(dut, _ROWS, field_mask=_Q1_MASK)
    assert words == _record_words(_ROWS, _Q1_MASK)


@cocotb.test()
async def torn_row_after_emitted_group_records_invalidates_the_batch(dut):
    # earlier groups close and drain as their keys change; a torn row later
    # in the batch must still resolve to a stream error with group_count 0 —
    # the error close-out invalidates the already-drained records
    await _start(dut)
    dut.cfg_field_mask.value = 0b0010
    dut.output_ready.value = 1
    good = [(1, 10, 0, 0), (1, 20, 0, 0), (2, 30, 0, 0)]
    send = cocotb.start_soon(_send_words(dut, _pack_rows(good) + [(999 << 32) | 3]))
    drained = []
    collect_done = False

    async def _drain():
        nonlocal collect_done
        for _ in range(4096):
            await ReadOnly()
            fired = int(dut.output_valid.value) == 1
            word = int(dut.output_data.value)
            await RisingEdge(dut.clk)
            if fired:
                drained.append(word)
            if int(dut.status_valid.value) == 1:
                break
        collect_done = True

    drain = cocotb.start_soon(_drain())
    await _expect_status(dut, error=True, code=GroupedFieldSumErrorCode.STREAM)
    await send
    await drain
    assert collect_done
    dut.output_ready.value = 0
    # the group-1 record drained before the error hit; the error close-out
    # is the invalidation signal
    assert drained == grouped_field_sum_rows(good[:2], field_mask=0b0010)[0].record_words()
    assert int(dut.group_count.value) == 0

    words = await _run(dut, _ROWS, field_mask=_Q1_MASK)
    assert words == _record_words(_ROWS, _Q1_MASK)


@cocotb.test()
async def error_status_survives_backpressure(dut):
    # while status_ready is held low the error close-out must stay stable
    # and the tile must refuse new input; recovery only after the handshake
    await _start(dut)
    dut.cfg_field_mask.value = _Q1_MASK
    dut.status_ready.value = 0
    await _send_words(dut, [(100 << 32) | 5])

    found = False
    for _ in range(8):
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if found:
            break
    assert found, "error status never asserted"
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "error status not held"
        assert int(dut.status_error.value) == 1
        assert int(dut.status_error_code.value) == GroupedFieldSumErrorCode.STREAM
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=True, code=GroupedFieldSumErrorCode.STREAM)
    words = await _run(dut, _ROWS, field_mask=_Q1_MASK)
    assert words == _record_words(_ROWS, _Q1_MASK)


@cocotb.test()
async def success_status_survives_backpressure(dut):
    # the success close-out must hold stable while status_ready is low and
    # the tile must refuse new input until the handshake completes
    await _start(dut)
    dut.cfg_field_mask.value = _Q1_MASK
    dut.status_ready.value = 0
    send = cocotb.start_soon(_send_rows(dut, _ROWS))
    dut.output_ready.value = 1
    words = []
    for _ in range(4096):
        await ReadOnly()
        fired = int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last:
                break
    await send
    dut.output_ready.value = 0
    assert words == _record_words(_ROWS, _Q1_MASK)

    found = False
    for _ in range(8):
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if found:
            break
    assert found, "success status never asserted"
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "success status not held"
        assert int(dut.status_error.value) == 0
        assert int(dut.status_error_code.value) == 0
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=False)
    words = await _run(dut, _ROWS, field_mask=_Q1_MASK)
    assert words == _record_words(_ROWS, _Q1_MASK)


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    # random group-run structure (runs of 1..5 equal keys, occasional key
    # reappearance), random masks and drain backpressure
    await _start(dut)
    rng = random.Random(0x51E1D2)
    for trial in range(40):
        rows = []
        keys = [rng.randint(-4, 4) for _ in range(rng.randint(1, 5))]
        for key in keys:
            for _ in range(rng.randint(1, 5)):
                row = [key]
                for _ in range(3):
                    if rng.random() < 0.1:
                        row.append(rng.choice((_INT32_MIN, _INT32_MAX, 0, -1)))
                    else:
                        row.append(rng.randint(-100000, 100000))
                rows.append(tuple(row))
        mask = rng.randint(1, 15)
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):  # a ready pattern must let data through eventually
            pattern = pattern + (1,)
        words = await _run(dut, rows, field_mask=mask, ready_pattern=pattern)
        golden = grouped_field_sum_rows(rows, field_mask=mask)
        assert words == _record_words(rows, mask), f"trial {trial}: rows={rows} mask={mask:#06b}"
        assert int(dut.group_count.value) == len(golden)


# ---- helpers ----


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.cfg_field_mask.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, *, field_mask: int, ready_pattern=(1,)):
    dut.cfg_field_mask.value = field_mask
    groups = grouped_field_sum_rows(rows, field_mask=field_mask)
    expect_words = len(groups) * (field_mask.bit_count() + 2)
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_output(dut, ready_pattern, expect_words=expect_words)
    await send
    return words


def _pack_rows(rows) -> list[int]:
    words = []
    for key, value, aux0, aux1 in rows:
        words.append(((value & _MASK32) << 32) | (key & _MASK32))
        words.append(((aux1 & _MASK32) << 32) | (aux0 & _MASK32))
    return words


async def _send_words(dut, words) -> None:
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


async def _send_rows(dut, rows) -> None:
    await _send_words(dut, _pack_rows(rows))


async def _collect_output(dut, ready_pattern, *, expect_words: int) -> list[int]:
    words: list[int] = []
    held = None
    for pattern_index in range(200000):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        dut.output_ready.value = ready
        await ReadOnly()
        valid = int(dut.output_valid.value) == 1
        fired = ready == 1 and valid
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        if held is not None:
            # valid/ready hold rule: a stalled beat must stay asserted and
            # byte-stable until it fires
            assert valid, "output_valid dropped while a beat was stalled"
            assert (word, last) == held, "output beat changed while stalled"
        held = None if (fired or not valid) else (word, last)
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                dut.output_ready.value = 0
                assert len(words) == expect_words, f"expected {expect_words} words, got {len(words)}"
                # the record drain ends with a success close-out status (the
                # record writer's done waits on a consumed producer status)
                await _expect_status(dut, error=False)
                await _assert_quiescent(dut)
                return words
    raise AssertionError(f"timed out collecting group records ({len(words)}/{expect_words} words)")


async def _assert_quiescent(dut) -> None:
    """After the batch's record stream drains, nothing more may appear on
    the output or status streams even with ready held high."""
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    for _ in range(6):
        await ReadOnly()
        assert int(dut.output_valid.value) == 0, "output beat after batch completion"
        assert int(dut.status_valid.value) == 0, "status beat after batch completion"
        await RisingEdge(dut.clk)
    dut.output_ready.value = 0
    dut.status_ready.value = 0


async def _expect_status(dut, *, error: bool, code: int = 0) -> None:
    dut.status_ready.value = 1
    for _ in range(4096):
        await ReadOnly()
        fired = int(dut.status_valid.value) == 1
        observed_error = int(dut.status_error.value)
        observed_code = int(dut.status_error_code.value)
        await RisingEdge(dut.clk)
        if fired:
            assert observed_error == int(error)
            if error:
                assert observed_code == code, f"status code {observed_code} != {code}"
            else:
                assert observed_code == 0, f"success close-out carried code {observed_code}"
            dut.status_ready.value = 0
            return
    raise AssertionError("timed out waiting for aggregation status")

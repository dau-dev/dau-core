from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import FieldSumErrorCode, field_sum_rows
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

_MASK32 = 0xFFFF_FFFF
_MASK64 = 0xFFFF_FFFF_FFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# (key, value, aux0, aux1) — the q6 field map is
# (shipdate-bucket, extendedprice, discount, quantity)
_ROWS = [
    (568, 1000, 5, 10),
    (569, 2000, 6, 30),
    (568, -1500, 7, 23),
    (570, -500, 6, 24),
    (568, 3000, 4, 1),
]

_VALUE_MASK = 0b0010  # q6: SUM(revenue) over the value field


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    dut.cfg_field_mask.value = _VALUE_MASK
    rows = [(index, index + 1, index + 2, index + 3) for index in range(256)]
    words = _pack_rows(rows)
    await assert_sustained_ingest(
        dut,
        "int32-field-sum-aggregation",
        send=lambda: _send_words(dut, words),
        input_beats=len(words),
        beats_per_row=2,
    )


@cocotb.test()
async def q6_single_sum_record(dut):
    await _start(dut)
    record = await _run(dut, _ROWS, field_mask=_VALUE_MASK)
    golden = field_sum_rows(_ROWS, field_mask=_VALUE_MASK)
    assert record == golden.record_words()
    assert record == [sum(r[1] for r in _ROWS) & _MASK64, len(_ROWS)]


@cocotb.test()
async def all_fields_mask_is_the_q1_shape(dut):
    # four sums + count from one config, not a new core
    await _start(dut)
    record = await _run(dut, _ROWS, field_mask=0b1111)
    assert record == field_sum_rows(_ROWS, field_mask=0b1111).record_words()
    assert len(record) == 5


@cocotb.test()
async def sparse_masks_emit_ascending_field_order(dut):
    await _start(dut)
    for mask in (0b0001, 0b0100, 0b1000, 0b0101, 0b1010, 0b1001, 0b0111):
        record = await _run(dut, _ROWS, field_mask=mask)
        assert record == field_sum_rows(_ROWS, field_mask=mask).record_words(), f"mask {mask:#06b}"


@cocotb.test()
async def negative_sums_are_twos_complement(dut):
    await _start(dut)
    rows = [(0, -1000, 0, 0), (0, -2000, 0, 0)]
    record = await _run(dut, rows, field_mask=_VALUE_MASK)
    assert record == [(-3000) & _MASK64, 2]


@cocotb.test()
async def int32_extreme_sums_accumulate_int64(dut):
    # INT32_MIN rows: the sum leaves the INT32 range and must stay exact
    await _start(dut)
    rows = [(_INT32_MIN, _INT32_MAX, _INT32_MIN, _INT32_MAX)] * 6
    record = await _run(dut, rows, field_mask=0b1111)
    golden = field_sum_rows(rows, field_mask=0b1111)
    assert record == golden.record_words()
    assert golden.sums[0] == 6 * _INT32_MIN


@cocotb.test()
async def single_row_batch(dut):
    await _start(dut)
    record = await _run(dut, [(1, 2, 3, 4)], field_mask=0b1111)
    assert record == [1, 2, 3, 4, 1]


@cocotb.test()
async def aggregated_count_reports_rows(dut):
    await _start(dut)
    await _run(dut, _ROWS, field_mask=_VALUE_MASK)
    assert int(dut.aggregated_count.value) == len(_ROWS)


@cocotb.test()
async def repeated_batches_relatch_the_mask(dut):
    await _start(dut)
    first = await _run(dut, _ROWS, field_mask=0b0010)
    second = await _run(dut, _ROWS, field_mask=0b1101)
    assert first == field_sum_rows(_ROWS, field_mask=0b0010).record_words()
    assert second == field_sum_rows(_ROWS, field_mask=0b1101).record_words()


@cocotb.test()
async def backpressure_on_the_record_drain(dut):
    await _start(dut)
    record = await _run(dut, _ROWS, field_mask=0b1111, ready_pattern=(1, 0, 0, 1, 0))
    assert record == field_sum_rows(_ROWS, field_mask=0b1111).record_words()


@cocotb.test()
async def zero_mask_drains_batch_then_reports_config_error(dut):
    await _start(dut)
    dut.cfg_field_mask.value = 0  # unconfigured tile fails explicitly
    send = cocotb.start_soon(_send_rows(dut, _ROWS))
    await _expect_status(dut, error=True, code=FieldSumErrorCode.CONFIG)
    await send
    assert int(dut.aggregated_count.value) == 0

    record = await _run(dut, _ROWS, field_mask=_VALUE_MASK)
    assert record == field_sum_rows(_ROWS, field_mask=_VALUE_MASK).record_words()


@cocotb.test()
async def torn_row_reports_stream_error(dut):
    await _start(dut)
    dut.cfg_field_mask.value = _VALUE_MASK
    # input_last on a w0: a torn row
    await _send_words(dut, [(100 << 32) | 5])
    await _expect_status(dut, error=True, code=FieldSumErrorCode.STREAM)
    assert int(dut.aggregated_count.value) == 0

    record = await _run(dut, _ROWS, field_mask=_VALUE_MASK)
    assert record == field_sum_rows(_ROWS, field_mask=_VALUE_MASK).record_words()


@cocotb.test()
async def torn_row_mid_batch_reports_stream_error(dut):
    await _start(dut)
    dut.cfg_field_mask.value = _VALUE_MASK
    send = cocotb.start_soon(_send_words(dut, _pack_rows(_ROWS[:3]) + [(999 << 32) | 999]))
    await _expect_status(dut, error=True, code=FieldSumErrorCode.STREAM)
    await send
    assert int(dut.aggregated_count.value) == 0

    record = await _run(dut, _ROWS, field_mask=_VALUE_MASK)
    assert record == field_sum_rows(_ROWS, field_mask=_VALUE_MASK).record_words()


@cocotb.test()
async def error_status_survives_backpressure(dut):
    # while status_ready is held low the error close-out must stay stable and
    # the tile must refuse new input; recovery only after the handshake
    await _start(dut)
    dut.cfg_field_mask.value = _VALUE_MASK
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
        assert int(dut.status_error_code.value) == FieldSumErrorCode.STREAM
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=True, code=FieldSumErrorCode.STREAM)
    record = await _run(dut, _ROWS, field_mask=_VALUE_MASK)
    assert record == field_sum_rows(_ROWS, field_mask=_VALUE_MASK).record_words()


@cocotb.test()
async def success_status_survives_backpressure(dut):
    # the success close-out must hold stable while status_ready is low and
    # the tile must refuse new input until the handshake completes
    await _start(dut)
    dut.cfg_field_mask.value = _VALUE_MASK
    dut.status_ready.value = 0
    send = cocotb.start_soon(_send_rows(dut, _ROWS))
    # drain the record with status_ready still low
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
    assert words == field_sum_rows(_ROWS, field_mask=_VALUE_MASK).record_words()

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
    record = await _run(dut, _ROWS, field_mask=_VALUE_MASK)
    assert record == field_sum_rows(_ROWS, field_mask=_VALUE_MASK).record_words()


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0x5F1E1D)
    for trial in range(40):
        rows = []
        for _ in range(rng.randint(1, 16)):
            row = []
            for _ in range(4):
                if rng.random() < 0.1:
                    row.append(rng.choice((_INT32_MIN, _INT32_MAX, 0, -1)))
                else:
                    row.append(rng.randint(-100000, 100000))
            rows.append(tuple(row))
        mask = rng.randint(1, 15)
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):  # a ready pattern must let data through eventually
            pattern = pattern + (1,)
        record = await _run(dut, rows, field_mask=mask, ready_pattern=pattern)
        golden = field_sum_rows(rows, field_mask=mask)
        assert record == golden.record_words(), f"trial {trial}: rows={rows} mask={mask:#06b}"
        assert int(dut.aggregated_count.value) == golden.count


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
    expect_words = field_mask.bit_count() + 1
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
    raise AssertionError(f"timed out collecting aggregation record ({len(words)}/{expect_words} words)")


async def _assert_quiescent(dut) -> None:
    """After a record drains, nothing more may appear on the output or
    status streams even with ready held high."""
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

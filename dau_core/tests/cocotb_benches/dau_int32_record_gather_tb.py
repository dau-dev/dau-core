from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import gather_records

IN_WORDS = int(os.environ.get("DAU_GATHER_IN_WORDS", "4"))
OUT_WORDS = int(os.environ.get("DAU_GATHER_OUT_WORDS", "1"))
BUCKET_BITS = 16
MASK64 = (1 << 64) - 1


def _selector(word: int, *, high: bool = False, shift: int = 0) -> int:
    return word | (int(high) << 6) | (shift << 7)


def _identity_selectors() -> tuple[int, ...]:
    return tuple(_selector((half // 2) % IN_WORDS, high=bool(half & 1)) for half in range(2 * OUT_WORDS))


@cocotb.test()
async def random_multi_record_batch_matches_golden(dut):
    await _start(dut)
    rng = random.Random(20260731 + IN_WORDS * 10 + OUT_WORDS)
    selectors = tuple(_selector((half * 3 + 1) % IN_WORDS, high=bool(half & 1), shift=(half * 7) % 32) for half in range(2 * OUT_WORDS))
    records = [[rng.getrandbits(64) for _ in range(IN_WORDS)] for _ in range(9)]
    output, lasts = await _run_clean(dut, records, selectors)
    assert output == gather_records([word for record in records for word in record], in_words=IN_WORDS, out_words=OUT_WORDS, selectors=selectors)
    assert lasts == [False] * (len(output) - 1) + [True]


@cocotb.test()
async def gather_holds_stable_under_backpressure(dut):
    await _start(dut)
    selectors = _identity_selectors()
    records = [[((word + 1) << 32) | (0xA0 + word) for word in range(IN_WORDS)]]
    _set_selectors(dut, selectors)
    dut.output_ready.value = 0
    send = cocotb.start_soon(_send_records(dut, records))
    await _wait_for(dut, "output_valid")
    held = (int(dut.output_data.value), int(dut.output_last.value))
    for _ in range(5):
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert int(dut.output_valid.value) == 1
        assert (int(dut.output_data.value), int(dut.output_last.value)) == held
    await RisingEdge(dut.clk)
    output, lasts = await _collect_output(dut, ready_pattern=(1, 0, 0, 1))
    await send
    assert output == gather_records(records[0], in_words=IN_WORDS, out_words=OUT_WORDS, selectors=selectors)
    assert lasts[-1] is True


@cocotb.test(skip=(IN_WORDS, OUT_WORDS) != (7, 3))
async def feature_to_bucket_matches_hero_projection(dut):
    await _start(dut)
    selectors = (
        _selector(1),
        _selector(1, high=True),
        _selector(0),
        _selector(0, high=True),
        _selector(3),
        _selector(5),
    )
    feature = [
        0x1122_3344_5566_7788,
        0x89AB_CDEF_0123_4567,
        0,
        0x8000_0002,
        0,
        0xFFFF_FFFA,
        0,
    ]
    output, _ = await _run_clean(dut, [feature, [word ^ MASK64 for word in feature]], selectors, ready_pattern=(1, 0, 1, 0, 0))
    assert output == gather_records(feature + [word ^ MASK64 for word in feature], in_words=7, out_words=3, selectors=selectors)
    assert output[:3] == [0x89AB_CDEF_0123_4567, 0x1122_3344_5566_7788, 0xFFFF_FFFA_8000_0002]


@cocotb.test(skip=(IN_WORDS, OUT_WORDS) != (11, 1))
async def bar_to_roll_matches_hero_logical_extract(dut):
    await _start(dut)
    selectors = (_selector(0, shift=BUCKET_BITS), _selector(1, high=True))
    bar = [0xF234_5678, 0x89AB_CDEF_1234_5678, *([0] * 9)]
    output, _ = await _run_clean(dut, [bar], selectors)
    assert output == gather_records(bar, in_words=11, out_words=1, selectors=selectors)
    assert output == [0x89AB_CDEF_0000_F234]


@cocotb.test(skip=IN_WORDS == 1)
async def torn_record_reports_stream_error(dut):
    await _start(dut)
    _set_selectors(dut, _identity_selectors())
    dut.output_ready.value = 1
    await _send_words(dut, [0x1234] * (IN_WORDS - 1), last_index=IN_WORDS - 2)
    await _expect_status(dut, code=2)
    assert int(dut.output_valid.value) == 0


@cocotb.test()
async def invalid_config_drains_batch_and_reports_config_error(dut):
    await _start(dut)
    selectors = list(_identity_selectors())
    selectors[0] |= 1 << 12
    _set_selectors(dut, tuple(selectors))
    dut.output_ready.value = 1
    words = [index * 0x1111 for index in range(2 * IN_WORDS)]
    await _send_words(dut, words, last_index=len(words) - 1)
    assert int(dut.output_valid.value) == 0
    await _expect_status(dut, code=1)

    selectors = list(_identity_selectors())
    selectors[0] = _selector(IN_WORDS)
    _set_selectors(dut, tuple(selectors))
    await _send_words(dut, words, last_index=len(words) - 1)
    assert int(dut.output_valid.value) == 0
    await _expect_status(dut, code=1)


@cocotb.test()
async def config_latches_for_entire_batch_and_reloads_between_batches(dut):
    await _start(dut)
    first = _identity_selectors()
    second = (_selector(0, shift=1), *first[1:])
    records = [[(record + 1) * 0x1234_5678_9ABC_DEF1 + word for word in range(IN_WORDS)] for record in range(2)]
    _set_selectors(dut, first)
    send = cocotb.start_soon(_send_records_with_config_change(dut, records, second))
    output, _ = await _collect_output(dut, ready_pattern=(1,))
    await send
    flat = [word & MASK64 for record in records for word in record]
    assert output == gather_records(flat, in_words=IN_WORDS, out_words=OUT_WORDS, selectors=first)

    output, _ = await _run_clean(dut, [records[0]], second)
    assert output == gather_records(records[0], in_words=IN_WORDS, out_words=OUT_WORDS, selectors=second)


@cocotb.test()
async def clean_batch_is_silent_on_success(dut):
    await _start(dut)
    records = [[index + 1 for index in range(IN_WORDS)]]
    await _run_clean(dut, records, _identity_selectors())
    dut.status_ready.value = 1
    for _ in range(16):
        await ReadOnly()
        assert int(dut.status_valid.value) == 0
        await RisingEdge(dut.clk)


async def _start(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    _set_selectors(dut, tuple(0 for _ in range(2 * OUT_WORDS)))
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


def _set_selectors(dut, selectors: tuple[int, ...]) -> None:
    assert len(selectors) == 2 * OUT_WORDS
    for index in range(16):
        getattr(dut, f"cfg_sel{index}").value = selectors[index] if index < len(selectors) else 0


async def _run_clean(dut, records, selectors, *, ready_pattern=(1,)):
    _set_selectors(dut, selectors)
    send = cocotb.start_soon(_send_records(dut, records))
    output, lasts = await _collect_output(dut, ready_pattern=ready_pattern)
    await send
    return output, lasts


async def _send_records(dut, records) -> None:
    words = [word & MASK64 for record in records for word in record]
    await _send_words(dut, words, last_index=len(words) - 1)


async def _send_records_with_config_change(dut, records, selectors) -> None:
    words = [word & MASK64 for record in records for word in record]
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
        if index == 0:
            _set_selectors(dut, selectors)
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _send_words(dut, words, *, last_index: int) -> None:
    for index, word in enumerate(words):
        dut.input_data.value = word
        dut.input_last.value = int(index == last_index)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _collect_output(dut, *, ready_pattern):
    words = []
    lasts = []
    for pattern_index in range(16384):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        dut.output_ready.value = ready
        await ReadOnly()
        fired = ready == 1 and int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = bool(int(dut.output_last.value))
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            lasts.append(last)
            if last:
                dut.output_ready.value = 0
                return words, lasts
    raise AssertionError("timed out collecting gather output")


async def _wait_for(dut, signal_name: str) -> None:
    for _ in range(512):
        await ReadOnly()
        if int(getattr(dut, signal_name).value) == 1:
            return
        await RisingEdge(dut.clk)
    raise AssertionError(f"timed out waiting for {signal_name}")


async def _expect_status(dut, *, code: int) -> None:
    dut.status_ready.value = 0
    await _wait_for(dut, "status_valid")
    assert int(dut.status_error.value) == 1
    assert int(dut.status_error_code.value) == code
    await RisingEdge(dut.clk)
    dut.status_ready.value = 1
    await RisingEdge(dut.clk)
    dut.status_ready.value = 0

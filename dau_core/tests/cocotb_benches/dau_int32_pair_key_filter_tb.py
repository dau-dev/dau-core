from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import filter_bar_rows_by_key

ERR_STREAM = 2

_ROWS = [
    (568, 100, 10, 500_000),
    (569, 105, 3, 0),
    (570, 95, 2, 250_000),
    (568, 102, 1, 999_999),
    (571, 102, 7, 0),
    (572, 99, 4, 100_000),
]


@cocotb.test()
async def filter_partitions_rows(dut):
    await _start(dut)
    for match in range(4):
        rows = await _run(dut, _ROWS, mask=3, match=match)
        assert rows == filter_bar_rows_by_key(_ROWS, key_mask=3, key_match=match), f"partition {match} mismatch"


@cocotb.test()
async def filter_last_lands_on_final_kept_row(dut):
    await _start(dut)
    # trailing rows drop: last must move onto the final forwarded word
    rows = [(568, 1, 1, 0), (569, 2, 2, 0), (571, 3, 3, 0)]
    assert await _run(dut, rows, mask=3, match=0) == [(568, 1, 1, 0)]


@cocotb.test()
async def filter_passes_everything_with_zero_mask(dut):
    await _start(dut)
    assert await _run(dut, _ROWS, mask=0, match=0) == _ROWS


@cocotb.test()
async def filter_backpressure_both_sides(dut):
    await _start(dut)
    rows = await _run(dut, _ROWS, mask=1, match=0, ready_pattern=(1, 0, 0, 1))
    assert rows == filter_bar_rows_by_key(_ROWS, key_mask=1, key_match=0)


@cocotb.test()
async def filter_empty_partition_closes_out(dut):
    await _start(dut)
    dut.cfg_key_mask.value = 3
    dut.cfg_key_match.value = 3  # nothing in _ROWS[:2] matches lane 3
    send = cocotb.start_soon(_send_rows(dut, _ROWS[:2]))
    await _expect_status(dut, error=False)
    await send


@cocotb.test()
async def filter_torn_row_reports_stream_error(dut):
    await _start(dut)
    dut.cfg_key_mask.value = 0
    dut.cfg_key_match.value = 0
    # input_last on a w0: a torn row
    dut.input_data.value = (100 << 32) | 568
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
    await _expect_status(dut, error=True, code=ERR_STREAM)

    # recovers: the next stream filters normally
    assert await _run(dut, _ROWS, mask=0, match=0) == _ROWS


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.cfg_key_mask.value = 0
    dut.cfg_key_match.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, *, mask: int, match: int, ready_pattern=(1,)):
    dut.cfg_key_mask.value = mask
    dut.cfg_key_match.value = match
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_output(dut, ready_pattern, expect_words=2 * len(filter_bar_rows_by_key(rows, key_mask=mask, key_match=match)))
    await send
    out = []
    for offset in range(0, len(words), 2):
        w0, w1 = words[offset], words[offset + 1]
        out.append((_signed32(w0 & 0xFFFFFFFF), _signed32(w0 >> 32), _signed32(w1 & 0xFFFFFFFF), _signed32(w1 >> 32)))
    return out


async def _send_rows(dut, rows) -> None:
    words = []
    for key, price, shares, frac in rows:
        words.append(((price & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF))
        words.append(((frac & 0xFFFFFFFF) << 32) | (shares & 0xFFFFFFFF))
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


async def _collect_output(dut, ready_pattern, *, expect_words: int) -> list[int]:
    words: list[int] = []
    if expect_words == 0:
        # the filter closes out with its success status instead
        await _expect_status(dut, error=False)
        return words
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
                assert len(words) == expect_words, f"expected {expect_words} words, got {len(words)}"
                return words
    raise AssertionError("timed out collecting filter output")


async def _expect_status(dut, *, error: bool, code: int = 0) -> None:
    dut.status_ready.value = 1
    for _ in range(128):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == int(error)
            if error:
                assert int(dut.status_error_code.value) == code
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for filter status")


def _signed32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value

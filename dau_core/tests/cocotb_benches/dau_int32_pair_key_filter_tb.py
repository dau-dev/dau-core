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


@cocotb.test()
async def status_close_out_waits_for_backpressured_tail(dut):
    # Cross-batch re-arm under output backpressure. Batch A forwards two kept
    # rows and its final beat is held under backpressure; batch B follows
    # immediately and would raise a success close-out (zero forwarded rows).
    # The filter must not present B's close-out on the status stream until A's
    # last output beat has handshaked -- otherwise B's per-batch close-out
    # overtakes A's still-in-flight tail (the lane status mux serialises one
    # close-out per batch, and the record writer's done waits on it).
    await _start(dut)
    dut.cfg_key_mask.value = 3
    dut.cfg_key_match.value = 0

    a_rows = [(568, 111, 1, 1), (572, 222, 2, 2)]  # both in lane 0: A forwards 2, last kept
    b_rows = [(569, 999, 9, 9)]  # lane 1: zero forwarded -> success close-out
    expected = [(word, int(i == 3)) for i, word in enumerate(_pack_rows(a_rows))]

    sender = cocotb.start_soon(_send_batches(dut, [_pack_rows(a_rows), _pack_rows(b_rows)]))
    dut.status_ready.value = 1  # a downstream mux ready to accept a close-out

    words: list[tuple[int, int]] = []
    fires = 0
    a_tail_done = False
    holding = False
    hold = 0
    for _ in range(400):
        if fires < 2:
            ready = 1  # drain A's first row and trigger the flush-load of its tail
        elif not holding and not a_tail_done:
            holding = True
            ready = 0
        elif holding and hold < 15:
            ready = 0
        else:
            ready = 1  # release: A's tail drains, then B's close-out may appear
        dut.output_ready.value = ready

        await ReadOnly()
        valid = int(dut.output_valid.value) == 1
        last = int(dut.output_last.value) == 1
        word = int(dut.output_data.value)
        status = int(dut.status_valid.value) == 1
        fired = ready == 1 and valid
        # THE PROPERTY: no status close-out while a prior batch's output beat is
        # still un-drained (A's final beat has not yet handshaked).
        assert not (status and not a_tail_done), (
            "batch B success close-out asserted (status_valid=1) before batch A's "
            "final output beat handshaked -- cross-batch status overtake under backpressure"
        )
        await RisingEdge(dut.clk)
        if holding:
            hold += 1
        if fired:
            words.append((word, last))
            fires += 1
            if last:
                a_tail_done = True
                break

    assert a_tail_done, "A's final beat never handshaked"
    assert words == expected, f"forwarded beats/last wrong: {words} != {expected}"

    # only now may B's success close-out appear
    await _expect_status(dut, error=False)
    await sender


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


def _pack_rows(rows) -> list[int]:
    words = []
    for key, price, shares, frac in rows:
        words.append(((price & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF))
        words.append(((frac & 0xFFFFFFFF) << 32) | (shares & 0xFFFFFFFF))
    return words


async def _send_batches(dut, batches) -> None:
    # drive several back-to-back batches with input_last on the final word of
    # each, holding input_valid across the boundaries so the next batch follows
    # the previous one with no gap
    for words in batches:
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
    words = _pack_rows(rows)
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

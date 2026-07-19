from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import (
    FilterErrorCode,
    FilterPredicate,
    PredicateCombine,
    PredicateOp,
    filter_rows,
)

_MASK32 = 0xFFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# one keyed (key, value) row per 64-bit word: {value[31:0], key[31:0]}
_ROWS = [
    (568, 1000),
    (569, 2000),
    (568, 1500),
    (570, -500),
    (568, 3000),
    (571, 2500),
]


@cocotb.test()
async def value_lane_single_and_combined_predicates(dut):
    await _start(dut)
    for predicate in (
        FilterPredicate(PredicateOp.GT, 1500),
        FilterPredicate(PredicateOp.GE, 0, PredicateCombine.AND, PredicateOp.LT, 2500),
        FilterPredicate(PredicateOp.LT, 0, PredicateCombine.OR, PredicateOp.GE, 2500),
    ):
        out, count = await _run(dut, _ROWS, predicate)
        expected = filter_rows(_ROWS, predicate)
        assert out == expected, predicate
        assert count == len(expected)


@cocotb.test()
async def key_lane_predicate_keeps_rows_aligned(dut):
    # cfg_target_lane=1 evaluates the predicate on the key lane; whole rows
    # pass or drop together (compaction never misaligns the lanes)
    await _start(dut)
    predicate = FilterPredicate(PredicateOp.EQ, 568)
    out, count = await _run(dut, _ROWS, predicate, on_key=True)
    expected = filter_rows(_ROWS, predicate, on_key=True)
    assert out == expected == [(568, 1000), (568, 1500), (568, 3000)]
    assert count == 3


@cocotb.test()
async def int32_extremes_compare_signed(dut):
    await _start(dut)
    rows = [(_INT32_MIN, _INT32_MAX), (_INT32_MAX, _INT32_MIN), (0, 0), (-1, 1)]
    for on_key in (False, True):
        for op in (PredicateOp.LT, PredicateOp.GE):
            for constant in (_INT32_MIN, -1, 0, _INT32_MAX):
                predicate = FilterPredicate(op, constant)
                out, _ = await _run(dut, rows, predicate, on_key=on_key)
                assert out == filter_rows(rows, predicate, on_key=on_key), f"{op.name} {constant} on_key={on_key}"


@cocotb.test()
async def all_dropped_still_closes_out_success(dut):
    # zero forwarded rows: no output words, but the tile still raises its
    # per-batch success close-out with selected_count == 0
    await _start(dut)
    predicate = FilterPredicate(PredicateOp.LT, _INT32_MIN)  # matches nothing
    out, count = await _run(dut, _ROWS, predicate)
    assert out == []
    assert count == 0


@cocotb.test()
async def last_driven_mode_trusts_upstream_last(dut):
    # cfg_row_count == 0: no stream-length check; the upstream last marker
    # decides the batch end (a stage downstream of another compaction)
    await _start(dut)
    predicate = FilterPredicate(PredicateOp.GE, 0)
    out, count = await _run(dut, _ROWS, predicate, count_checked=False)
    expected = filter_rows(_ROWS, predicate)
    assert out == expected
    assert count == len(expected)


@cocotb.test()
async def backpressure_holds_output_stable(dut):
    await _start(dut)
    predicate = FilterPredicate(PredicateOp.NE, 0)
    out, count = await _run(dut, _ROWS, predicate, ready_pattern=(1, 0, 0, 1, 0))
    expected = filter_rows(_ROWS, predicate)
    assert out == expected
    assert count == len(expected)


@cocotb.test()
async def reset_clears_state(dut):
    # drive two matching rows without a batch terminator so a partial result
    # accumulates (selected_count, pending, a stalled output beat under
    # backpressure), then assert rst and confirm the tile is fully quiescent.
    # A third row would jam the sender: the emit of the first match holds
    # output_valid, and with output_ready low the tile stops accepting input.
    await _start(dut)
    _drive_config(dut, FilterPredicate(PredicateOp.GE, 0), on_key=False, row_count=0)
    dut.output_ready.value = 0
    await _send_words(dut, _pack_rows(_ROWS[:2]), last_on_final=False)
    await ReadOnly()
    assert int(dut.selected_count.value) > 0, "no partial state accumulated to reset"
    await RisingEdge(dut.clk)

    dut.rst.value = 1
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.selected_count.value) == 0
    assert int(dut.output_valid.value) == 0
    assert int(dut.status_valid.value) == 0
    await RisingEdge(dut.clk)

    # a fresh batch runs cleanly, counting from zero
    predicate = FilterPredicate(PredicateOp.EQ, 568)
    out, count = await _run(dut, _ROWS, predicate, on_key=True)
    assert out == filter_rows(_ROWS, predicate, on_key=True)
    assert count == 3


@cocotb.test()
async def short_stream_reports_stream_error(dut):
    # count-checked mode: input_last before the declared count is a STREAM
    # error (the row-per-word analog of a torn row)
    await _start(dut)
    _drive_config(dut, FilterPredicate(PredicateOp.LT, _INT32_MIN), on_key=False, row_count=len(_ROWS) + 2)
    dut.output_ready.value = 1
    await _send_words(dut, _pack_rows(_ROWS))  # last after fewer rows than declared
    await _expect_error(dut, FilterErrorCode.STREAM)
    assert int(dut.selected_count.value) == 0
    await _expect_recovers(dut)


@cocotb.test()
async def long_stream_reports_stream_error(dut):
    # count-checked mode: more rows than the declared count -> the count is
    # reached with no last marker, then a trailing last closes the error
    await _start(dut)
    _drive_config(dut, FilterPredicate(PredicateOp.LT, _INT32_MIN), on_key=False, row_count=2)
    dut.output_ready.value = 1
    await _send_words(dut, _pack_rows(_ROWS[:4]))  # 4 rows declared as 2
    await _expect_error(dut, FilterErrorCode.STREAM)
    await _expect_recovers(dut)


@cocotb.test()
async def reserved_op_reports_config_error(dut):
    await _start(dut)
    _drive_config(dut, FilterPredicate(PredicateOp.EQ, 0), on_key=False, row_count=len(_ROWS))
    dut.cfg_op0.value = 0  # reserved: an unconfigured tile fails explicitly
    dut.output_ready.value = 1
    await _send_words(dut, _pack_rows(_ROWS))
    await _expect_error(dut, FilterErrorCode.CONFIG)
    await _expect_recovers(dut)


@cocotb.test()
async def reserved_combine_op_reports_config_error(dut):
    # combine == AND with a reserved op1 is an invalid predicate tree
    await _start(dut)
    _drive_config(dut, FilterPredicate(PredicateOp.EQ, 568), on_key=False, row_count=len(_ROWS))
    dut.cfg_combine.value = int(PredicateCombine.AND)
    dut.cfg_op1.value = 0
    dut.output_ready.value = 1
    await _send_words(dut, _pack_rows(_ROWS))
    await _expect_error(dut, FilterErrorCode.CONFIG)


@cocotb.test()
async def error_status_survives_backpressure(dut):
    # while status_ready is held low the error close-out stays stable and the
    # tile refuses new input; recovery only after the handshake
    await _start(dut)
    _drive_config(dut, FilterPredicate(PredicateOp.GE, 0), on_key=False, row_count=len(_ROWS) + 2)
    dut.status_ready.value = 0
    dut.output_ready.value = 1
    await _send_words(dut, _pack_rows(_ROWS))

    await _wait_status_asserted(dut)
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "error status not held"
        assert int(dut.status_error.value) == 1
        assert int(dut.status_error_code.value) == FilterErrorCode.STREAM
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_error(dut, FilterErrorCode.STREAM)
    await _expect_recovers(dut)


@cocotb.test()
async def success_status_survives_backpressure(dut):
    # the per-batch success close-out must hold stable while status_ready is
    # low and the tile must refuse new input until the handshake completes
    await _start(dut)
    predicate = FilterPredicate(PredicateOp.EQ, 568)
    _drive_config(dut, predicate, on_key=True, row_count=len(_ROWS))
    dut.status_ready.value = 0
    dut.output_ready.value = 1
    await _send_words(dut, _pack_rows(_ROWS))

    await _wait_status_asserted(dut)
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "success status not held"
        assert int(dut.status_error.value) == 0
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)

    count = await _accept_success_status(dut)
    assert count == 3
    await _expect_recovers(dut)


@cocotb.test()
async def repeated_batches_relatch_config(dut):
    # back-to-back batches with distinct configs each latch fresh at IDLE
    await _start(dut)
    for predicate, on_key in (
        (FilterPredicate(PredicateOp.LT, 0), False),
        (FilterPredicate(PredicateOp.EQ, 568), True),
        (FilterPredicate(PredicateOp.GE, 2000), False),
    ):
        out, count = await _run(dut, _ROWS, predicate, on_key=on_key)
        expected = filter_rows(_ROWS, predicate, on_key=on_key)
        assert out == expected, (predicate, on_key)
        assert count == len(expected)


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0xC0FFEE)
    ops = list(PredicateOp)
    combines = list(PredicateCombine)
    for trial in range(40):
        rows = []
        for _ in range(rng.randint(1, 12)):
            row = tuple(rng.choice((_INT32_MIN, _INT32_MAX, 0, -1)) if rng.random() < 0.1 else rng.randint(-40, 40) for _ in range(2))
            rows.append(row)

        def constant():
            return rng.choice((_INT32_MIN, _INT32_MAX, 0)) if rng.random() < 0.15 else rng.randint(-40, 40)

        combine = rng.choice(combines)
        predicate = FilterPredicate(
            op0=rng.choice(ops),
            constant0=constant(),
            combine=combine,
            op1=rng.choice(ops),
            constant1=constant(),
        )
        on_key = bool(rng.getrandbits(1))
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):
            pattern = pattern + (1,)
        out, count = await _run(dut, rows, predicate, on_key=on_key, ready_pattern=pattern)
        expected = filter_rows(rows, predicate, on_key=on_key)
        assert out == expected, f"trial {trial}: rows={rows} predicate={predicate} on_key={on_key}"
        assert count == len(expected)


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _drive_config(dut, predicate: FilterPredicate, *, on_key: bool, row_count: int) -> None:
    dut.cfg_target_lane.value = int(on_key)
    dut.cfg_op0.value = int(predicate.op0)
    dut.cfg_constant0.value = predicate.constant0 & _MASK32
    dut.cfg_combine.value = int(predicate.combine)
    dut.cfg_op1.value = int(predicate.op1)
    dut.cfg_constant1.value = predicate.constant1 & _MASK32
    dut.cfg_row_count.value = row_count


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    _drive_config(dut, FilterPredicate(PredicateOp.EQ, 0), on_key=False, row_count=0)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, predicate: FilterPredicate, *, on_key=False, count_checked=True, ready_pattern=(1,)):
    _drive_config(dut, predicate, on_key=on_key, row_count=len(rows) if count_checked else 0)
    expected = filter_rows(rows, predicate, on_key=on_key)
    send = cocotb.start_soon(_send_words(dut, _pack_rows(rows)))
    words, count = await _collect_forwarded(dut, ready_pattern, expect_rows=len(expected))
    await send
    out = [(_signed32(word & _MASK32), _signed32(word >> 32)) for word in words]
    return out, count


def _pack_rows(rows) -> list[int]:
    return [((value & _MASK32) << 32) | (key & _MASK32) for key, value in rows]


async def _send_words(dut, words, *, last_on_final: bool = True) -> None:
    for index, word in enumerate(words):
        dut.input_data.value = word
        dut.input_last.value = int(last_on_final and index == len(words) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _collect_forwarded(dut, ready_pattern, *, expect_rows: int) -> tuple[list[int], int]:
    words: list[int] = []
    if expect_rows > 0:
        pattern_index = 0
        held = None
        collected = False
        for _ in range(200000):
            ready = ready_pattern[pattern_index % len(ready_pattern)]
            pattern_index += 1
            dut.output_ready.value = ready
            await ReadOnly()
            valid = int(dut.output_valid.value) == 1
            fired = ready == 1 and valid
            word = int(dut.output_data.value)
            last = int(dut.output_last.value)
            if held is not None:
                # valid/ready hold rule: a stalled beat stays asserted and
                # byte-stable until it fires
                assert valid, "output_valid dropped while a beat was stalled"
                assert (word, last) == held, "output beat changed while stalled"
            held = None if (fired or not valid) else (word, last)
            await RisingEdge(dut.clk)
            if fired:
                words.append(word)
                if last == 1:
                    assert len(words) == expect_rows, f"expected {expect_rows} rows, got {len(words)}"
                    collected = True
                    break
        if not collected:
            raise AssertionError(f"timed out collecting filter output ({len(words)}/{expect_rows} rows)")
    # every batch (forwarding or empty) ends in a success close-out
    count = await _accept_success_status(dut)
    return words, count


async def _accept_success_status(dut) -> int:
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    for _ in range(4096):
        await ReadOnly()
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0, "success close-out flagged an error"
            assert int(dut.status_error_code.value) == 0
            count = int(dut.selected_count.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            dut.output_ready.value = 0
            return count
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


async def _wait_status_asserted(dut) -> None:
    # return in the post-clock phase (not ReadOnly) so the caller may ReadOnly
    for _ in range(16):
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if found:
            return
    raise AssertionError("status never asserted")


async def _expect_error(dut, code: FilterErrorCode) -> None:
    dut.status_ready.value = 1
    for _ in range(4096):
        await ReadOnly()
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == int(code), f"status code {int(dut.status_error_code.value)} != {int(code)}"
            assert int(dut.output_valid.value) == 0, "error close-out left an output beat in flight"
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for error status")


async def _expect_recovers(dut) -> None:
    # after any close-out the tile returns to IDLE and filters the next batch
    predicate = FilterPredicate(PredicateOp.EQ, 568)
    out, count = await _run(dut, _ROWS, predicate, on_key=True)
    assert out == filter_rows(_ROWS, predicate, on_key=True)
    assert count == 3

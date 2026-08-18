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
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

_MASK32 = 0xFFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# (key, value, aux0, aux1) — the q6 field map is
# (shipdate-bucket, extendedprice, discount, quantity)
_ROWS = [
    (568, 1000, 5, 10),
    (569, 2000, 6, 30),
    (568, 1500, 7, 23),
    (570, -500, 6, 24),
    (568, 3000, 4, 1),
    (568, 2500, 6, 40),
]

_Q6_PREDICATE = RowPredicateConfig(
    term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568),
    combine01=PredicateCombine.AND,
    term1=RowPredicateTerm(field=RowField.AUX0, op=PredicateOp.GE, constant=5),
    combine2=PredicateCombine.AND,
    term2=RowPredicateTerm(field=RowField.AUX1, op=PredicateOp.LT, constant=24),
)


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    rows = [(index, index + 1, index + 2, index + 3) for index in range(256)]
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.GE, constant=0))
    _drive_config(dut, config)
    words = _pack_rows(rows)
    await assert_sustained_ingest(
        dut,
        "row-predicate-filter",
        send=lambda: _send_words(dut, words),
        input_beats=len(words),
        beats_per_row=2,
    )


@cocotb.test()
async def single_term_on_each_field(dut):
    await _start(dut)
    for field, op, constant in (
        (RowField.KEY, PredicateOp.EQ, 568),
        (RowField.VALUE, PredicateOp.GT, 1500),
        (RowField.AUX0, PredicateOp.LE, 5),
        (RowField.AUX1, PredicateOp.NE, 30),
    ):
        config = RowPredicateConfig(term0=RowPredicateTerm(field=field, op=op, constant=constant))
        assert await _run(dut, _ROWS, config) == filter_rows_by_predicate(_ROWS, config), f"{field.name} {op.name}"


@cocotb.test()
async def two_term_and_is_range_filter(dut):
    await _start(dut)
    config = RowPredicateConfig(
        term0=RowPredicateTerm(field=RowField.AUX0, op=PredicateOp.GE, constant=5),
        combine01=PredicateCombine.AND,
        term1=RowPredicateTerm(field=RowField.AUX0, op=PredicateOp.LE, constant=6),
    )
    out = await _run(dut, _ROWS, config)
    assert out == filter_rows_by_predicate(_ROWS, config)
    assert all(5 <= row[2] <= 6 for row in out)


@cocotb.test()
async def three_term_q6_where_clause(dut):
    await _start(dut)
    out = await _run(dut, _ROWS, _Q6_PREDICATE)
    assert out == filter_rows_by_predicate(_ROWS, _Q6_PREDICATE)
    assert out == [(568, 1000, 5, 10), (568, 1500, 7, 23)]


@cocotb.test()
async def or_of_and_tree(dut):
    # (value > 2000 OR key == 570) AND aux1 < 30: exercises both combine levels
    await _start(dut)
    config = RowPredicateConfig(
        term0=RowPredicateTerm(field=RowField.VALUE, op=PredicateOp.GT, constant=2000),
        combine01=PredicateCombine.OR,
        term1=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=570),
        combine2=PredicateCombine.AND,
        term2=RowPredicateTerm(field=RowField.AUX1, op=PredicateOp.LT, constant=30),
    )
    assert await _run(dut, _ROWS, config) == filter_rows_by_predicate(_ROWS, config)


@cocotb.test()
async def last_lands_on_final_kept_row(dut):
    # trailing rows drop: last must move onto the final forwarded word
    await _start(dut)
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568))
    rows = [(568, 1, 1, 1), (569, 2, 2, 2), (570, 3, 3, 3)]
    assert await _run(dut, rows, config) == [(568, 1, 1, 1)]


@cocotb.test()
async def zero_rows_closes_out_success(dut):
    await _start(dut)
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=999))
    assert await _run(dut, _ROWS, config) == []
    assert int(dut.selected_count.value) == 0


@cocotb.test()
async def selected_count_reports_forwarded_rows(dut):
    await _start(dut)
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568))
    out = await _run(dut, _ROWS, config)
    assert int(dut.selected_count.value) == len(out) == 4


@cocotb.test()
async def int32_extremes_compare_signed(dut):
    await _start(dut)
    rows = [(_INT32_MIN, _INT32_MAX, -1, 0), (_INT32_MAX, _INT32_MIN, 1, -1), (0, 0, 0, 0)]
    for field in RowField:
        for op in (PredicateOp.LT, PredicateOp.GE):
            for constant in (_INT32_MIN, -1, 0, _INT32_MAX):
                config = RowPredicateConfig(term0=RowPredicateTerm(field=field, op=op, constant=constant))
                assert await _run(dut, rows, config) == filter_rows_by_predicate(rows, config), f"{field.name} {op.name} {constant}"


@cocotb.test()
async def backpressure_on_the_output(dut):
    await _start(dut)
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.AUX1, op=PredicateOp.LT, constant=30))
    out = await _run(dut, _ROWS, config, ready_pattern=(1, 0, 0, 1, 0))
    assert out == filter_rows_by_predicate(_ROWS, config)


@cocotb.test()
async def torn_row_reports_stream_error(dut):
    await _start(dut)
    _drive_config(dut, RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.GE, constant=0)))
    # input_last on a w0: a torn row
    await _send_torn_w0(dut)
    await _expect_status(dut, error=True, code=FilterErrorCode.STREAM)
    assert int(dut.selected_count.value) == 0

    # recovers: the next batch filters normally
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568))
    assert await _run(dut, _ROWS, config) == filter_rows_by_predicate(_ROWS, config)


@cocotb.test()
async def torn_row_mid_batch_reports_stream_error(dut):
    # a torn final row invalidates the batch even after forwarded rows
    await _start(dut)
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.GE, constant=0))
    _drive_config(dut, config)
    send = cocotb.start_soon(_send_rows_then_torn_w0(dut, _ROWS[:2]))
    collect = cocotb.start_soon(_drain_words(dut))
    await _expect_status(dut, error=True, code=FilterErrorCode.STREAM)
    await send
    collect.kill()
    dut.output_ready.value = 0
    assert int(dut.selected_count.value) == 0
    assert await _run(dut, _ROWS, config) == filter_rows_by_predicate(_ROWS, config)


@cocotb.test()
async def invalid_op_drains_batch_then_reports_config_error(dut):
    await _start(dut)
    _drive_config(dut, RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=0)))
    dut.cfg_op0.value = 0  # reserved: an unconfigured tile fails explicitly
    send = cocotb.start_soon(_send_rows(dut, _ROWS))
    await _expect_status(dut, error=True, code=FilterErrorCode.CONFIG)
    await send

    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568))
    assert await _run(dut, _ROWS, config) == filter_rows_by_predicate(_ROWS, config)


@cocotb.test()
async def invalid_combine_op_reports_config_error(dut):
    # combine01 = AND with a reserved op1 is an invalid predicate tree
    await _start(dut)
    _drive_config(dut, RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568)))
    dut.cfg_combine01.value = int(PredicateCombine.AND)
    dut.cfg_op1.value = 0
    send = cocotb.start_soon(_send_rows(dut, _ROWS[:2]))
    await _expect_status(dut, error=True, code=FilterErrorCode.CONFIG)
    await send


@cocotb.test()
async def error_status_survives_backpressure(dut):
    # while status_ready is held low the error close-out must stay stable and
    # the filter must refuse new input; recovery only after the handshake
    await _start(dut)
    _drive_config(dut, RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.GE, constant=0)))
    dut.status_ready.value = 0
    await _send_torn_w0(dut)

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
        assert int(dut.status_error_code.value) == FilterErrorCode.STREAM
        assert int(dut.input_ready.value) == 0, "filter accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=True, code=FilterErrorCode.STREAM)
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568))
    assert await _run(dut, _ROWS, config) == filter_rows_by_predicate(_ROWS, config)


@cocotb.test()
async def zero_rows_success_status_survives_backpressure(dut):
    # the zero-rows success close-out must hold stable while status_ready is
    # low and the filter must refuse new input until the handshake completes
    await _start(dut)
    _drive_config(dut, RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=999)))
    dut.status_ready.value = 0
    await _send_rows(dut, _ROWS)

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
        assert int(dut.input_ready.value) == 0, "filter accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=False)
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568))
    assert await _run(dut, _ROWS, config) == filter_rows_by_predicate(_ROWS, config)


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
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568))
    _drive_config(dut, config)

    a_rows = [(568, 111, 1, 1), (568, 222, 2, 2)]  # both match: A forwards 2, last kept
    b_rows = [(569, 999, 9, 9)]  # no match: zero forwarded -> success close-out
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

    # only now may B's success close-out appear; then the lane is quiescent
    await _expect_status(dut, error=False)
    assert int(dut.selected_count.value) == 0
    await _assert_quiescent(dut)
    await sender


@cocotb.test()
async def error_close_out_waits_for_backpressured_tail(dut):
    # Intra-batch error ordering under output backpressure. Two matching rows
    # are driven, then a torn w0 (input_last on a w0) raises ERR_STREAM.
    # Accepting the second match displaces the first into the emit engine,
    # where it stalls under backpressure; the torn w0 is then accepted (word-0
    # state is unconditionally ready) and raises the error. The error close-out
    # must not handshake until that stalled beat drains -- otherwise the error
    # status overtakes a still-valid output beat, which the record writer would
    # strand into the next job once it consumes the status.
    await _start(dut)
    config = RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568))
    _drive_config(dut, config)

    dut.output_ready.value = 0
    dut.status_ready.value = 1
    # two matches, then a torn w0; all accepted while output is backpressured
    words = _pack_rows([(568, 111, 1, 1), (568, 222, 2, 2)]) + [(999 << 32) | 999]
    await _send_words(dut, words)

    for _ in range(15):
        await ReadOnly()
        assert int(dut.output_valid.value) == 1, "the matched tail is not in flight"
        assert int(dut.status_valid.value) == 0, (
            "ERR_STREAM close-out asserted while a prior matched beat is still un-drained -- intra-batch error overtake under backpressure"
        )
        await RisingEdge(dut.clk)

    dut.output_ready.value = 1
    await _expect_status(dut, error=True, code=FilterErrorCode.STREAM)
    assert int(dut.selected_count.value) == 0
    # recovers: the next batch filters normally (the drained stalled beat did
    # not leak into it)
    assert await _run(dut, _ROWS, config) == filter_rows_by_predicate(_ROWS, config)


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0xF117E4)
    fields = list(RowField)
    ops = list(PredicateOp)
    combines = list(PredicateCombine)
    for trial in range(40):
        rows = []
        for _ in range(rng.randint(1, 12)):
            row = []
            for _ in range(4):
                if rng.random() < 0.1:
                    row.append(rng.choice((_INT32_MIN, _INT32_MAX, 0, -1)))
                else:
                    row.append(rng.randint(-40, 40))
            rows.append(tuple(row))

        def term():
            constant = rng.choice((_INT32_MIN, _INT32_MAX, 0)) if rng.random() < 0.15 else rng.randint(-40, 40)
            return RowPredicateTerm(field=rng.choice(fields), op=rng.choice(ops), constant=constant)

        combine01 = rng.choice(combines)
        combine2 = rng.choice(combines)
        config = RowPredicateConfig(
            term0=term(),
            combine01=combine01,
            term1=term() if combine01 != PredicateCombine.SINGLE else None,
            combine2=combine2,
            term2=term() if combine2 != PredicateCombine.SINGLE else None,
        )
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):  # a ready pattern must let data through eventually
            pattern = pattern + (1,)
        out = await _run(dut, rows, config, ready_pattern=pattern)
        expected = filter_rows_by_predicate(rows, config)
        assert out == expected, f"trial {trial}: rows={rows} config={config}"
        assert int(dut.selected_count.value) == len(expected)


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


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
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
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


async def _run(dut, rows, config: RowPredicateConfig, *, ready_pattern=(1,)):
    _drive_config(dut, config)
    expected = filter_rows_by_predicate(rows, config)
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_output(dut, ready_pattern, expect_words=2 * len(expected))
    await send
    out = []
    for offset in range(0, len(words), 2):
        w0, w1 = words[offset], words[offset + 1]
        out.append((_signed32(w0 & _MASK32), _signed32(w0 >> 32), _signed32(w1 & _MASK32), _signed32(w1 >> 32)))
    return out


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


async def _send_rows_then_torn_w0(dut, rows) -> None:
    """Send complete rows (no last), then a lone last-carrying w0."""
    await _send_words(dut, _pack_rows(rows) + [(999 << 32) | 999])


async def _send_torn_w0(dut) -> None:
    await _send_words(dut, [(100 << 32) | 5])


async def _drain_words(dut) -> None:
    dut.output_ready.value = 1
    while True:
        await RisingEdge(dut.clk)


async def _collect_output(dut, ready_pattern, *, expect_words: int) -> list[int]:
    words: list[int] = []
    if expect_words == 0:
        # zero forwarded rows: the filter closes out with a success status
        await _expect_status(dut, error=False)
        await _assert_quiescent(dut)
        return words
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
                # rows forwarded: the filter stays silent (the downstream
                # tile's status is authoritative)
                await _assert_quiescent(dut)
                return words
    raise AssertionError(f"timed out collecting filter output ({len(words)}/{expect_words} words)")


async def _assert_quiescent(dut) -> None:
    """After a batch completes, nothing more may appear on the output or
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
    raise AssertionError("timed out waiting for filter status")

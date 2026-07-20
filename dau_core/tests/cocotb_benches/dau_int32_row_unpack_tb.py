from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import UnpackErrorCode, UnpackFieldSpec, unpack_rows

_MASK32 = 0xFFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# byte-lane packing (32 of 64 bits used): the simplest layout
_BYTE_SPECS = [UnpackFieldSpec(offset=o, width=8) for o in (0, 8, 16, 24)]

# q6-like packing: (shipdate-bucket, extendedprice, discount, quantity) into 59
# bits — 16 + 25 + 10 + 8, offsets 0/16/41/51
_Q6_SPECS = [
    UnpackFieldSpec(offset=0, width=16),
    UnpackFieldSpec(offset=16, width=25, signed=True),
    UnpackFieldSpec(offset=41, width=10),
    UnpackFieldSpec(offset=51, width=8),
]
# q1-like packing: 63 bits — 10 + 32 + 13 + 8, offsets 0/10/42/55
_Q1_SPECS = [
    UnpackFieldSpec(offset=0, width=10),
    UnpackFieldSpec(offset=10, width=32, signed=True),
    UnpackFieldSpec(offset=42, width=13),
    UnpackFieldSpec(offset=55, width=8),
]

_Q6_ROWS = [(568, 1000, 5, 10), (569, -500, 6, 30), (570, 12000, 1023, 255), (568, 250, 0, 1)]


@cocotb.test()
async def byte_lane_unpack_matches_golden(dut):
    await _start(dut)
    rows = [(0x11, 0x22, 0x33, 0x44), (0x00, 0xFF, 0x80, 0x7F)]
    words = [_pack(_BYTE_SPECS, row) for row in rows]
    assert await _run(dut, words, _BYTE_SPECS) == unpack_rows(words, _BYTE_SPECS)


@cocotb.test()
async def sign_versus_zero_extend(dut):
    await _start(dut)
    signed = [UnpackFieldSpec(offset=o, width=8, signed=True) for o in (0, 8, 16, 24)]
    unsigned = [UnpackFieldSpec(offset=o, width=8) for o in (0, 8, 16, 24)]
    rows = [(-1, -128, 127, 0), (0xFF, 0x80, 0x7F, 0x01)]
    for specs, fields in ((signed, rows[0]), (unsigned, rows[1])):
        word = _pack(specs, fields)
        assert await _run(dut, [word], specs) == unpack_rows([word], specs)


@cocotb.test()
async def fields_crossing_bit_boundaries(dut):
    await _start(dut)
    specs = [
        UnpackFieldSpec(offset=3, width=13),
        UnpackFieldSpec(offset=16, width=17, signed=True),
        UnpackFieldSpec(offset=33, width=11),
        UnpackFieldSpec(offset=44, width=20, signed=True),
    ]
    rows = [(0x1ABC & 0x1FFF, -12345, 0x5A5, -7), (0, 0, 0, 0), (0x1FFF, _int_in_width(17), 0x7FF, _int_in_width(20))]
    words = [_pack(specs, row) for row in rows]
    assert await _run(dut, words, specs) == unpack_rows(words, specs)


@cocotb.test()
async def boundary_widths_one_and_thirtytwo(dut):
    await _start(dut)
    specs = [
        UnpackFieldSpec(offset=0, width=1, signed=True),
        UnpackFieldSpec(offset=1, width=1),
        UnpackFieldSpec(offset=2, width=32, signed=True),
        UnpackFieldSpec(offset=34, width=30),
    ]
    rows = [(-1, 1, -(2**31), (1 << 30) - 1), (0, 0, 2**31 - 1, 0), (-1, 0, -1, (1 << 30) - 1)]
    words = [_pack(specs, row) for row in rows]
    assert await _run(dut, words, specs) == unpack_rows(words, specs)


@cocotb.test()
async def width32_unsigned_reads_back_signed(dut):
    await _start(dut)
    specs = [
        UnpackFieldSpec(offset=0, width=32),
        UnpackFieldSpec(offset=32, width=32, signed=True),
        UnpackFieldSpec(offset=0, width=1),
        UnpackFieldSpec(offset=1, width=1),
    ]
    word = (0xFFFF_FFFF << 32) | 0x8000_0000
    assert await _run(dut, [word], specs) == unpack_rows([word], specs)


@cocotb.test()
async def q6_like_packing(dut):
    await _start(dut)
    words = [_pack(_Q6_SPECS, row) for row in _Q6_ROWS]
    out = await _run(dut, words, _Q6_SPECS)
    assert out == unpack_rows(words, _Q6_SPECS)
    assert out[0] == (568, 1000, 5, 10)


@cocotb.test()
async def q1_like_packing(dut):
    await _start(dut)
    rows = [(3, 2**31 - 1, 8191, 7), (3, -(2**31), 0, 255), (9, -1, 4096, 1)]
    words = [_pack(_Q1_SPECS, row) for row in rows]
    assert await _run(dut, words, _Q1_SPECS) == unpack_rows(words, _Q1_SPECS)


@cocotb.test()
async def single_row_batch(dut):
    await _start(dut)
    word = _pack(_Q6_SPECS, (570, 42, 3, 9))
    assert await _run(dut, [word], _Q6_SPECS) == unpack_rows([word], _Q6_SPECS)


@cocotb.test()
async def output_backpressure_holds_stable(dut):
    await _start(dut)
    words = [_pack(_Q6_SPECS, row) for row in _Q6_ROWS]
    out = await _run(dut, words, _Q6_SPECS, ready_pattern=(1, 0, 0, 1, 0))
    assert out == unpack_rows(words, _Q6_SPECS)


@cocotb.test()
async def repeated_batches_back_to_back(dut):
    await _start(dut)
    for _ in range(3):
        words = [_pack(_Q6_SPECS, row) for row in _Q6_ROWS]
        assert await _run(dut, words, _Q6_SPECS) == unpack_rows(words, _Q6_SPECS)


@cocotb.test()
async def bad_config_zero_width_drains_then_config_error(dut):
    await _start(dut)
    # field1 width 0 is invalid
    _drive_raw_config(dut, [(0, 8, 0), (8, 0, 0), (16, 8, 0), (24, 8, 0)])
    send = cocotb.start_soon(_send_words(dut, [0x1234_5678, 0x9ABC_DEF0]))
    await _expect_status(dut, error=True, code=UnpackErrorCode.CONFIG)
    await send
    # recovers
    words = [_pack(_BYTE_SPECS, (1, 2, 3, 4))]
    assert await _run(dut, words, _BYTE_SPECS) == unpack_rows(words, _BYTE_SPECS)


@cocotb.test()
async def bad_config_width_over_32_reports_config_error(dut):
    await _start(dut)
    _drive_raw_config(dut, [(0, 8, 0), (8, 8, 0), (16, 33, 0), (24, 8, 0)])
    send = cocotb.start_soon(_send_words(dut, [0xDEAD_BEEF]))  # single-row batch
    await _expect_status(dut, error=True, code=UnpackErrorCode.CONFIG)
    await send
    words = [_pack(_BYTE_SPECS, (5, 6, 7, 8))]
    assert await _run(dut, words, _BYTE_SPECS) == unpack_rows(words, _BYTE_SPECS)


@cocotb.test()
async def bad_config_offset_plus_width_over_64_reports_config_error(dut):
    await _start(dut)
    # field3 runs past bit 64 (50 + 20 = 70)
    _drive_raw_config(dut, [(0, 8, 0), (8, 8, 0), (16, 8, 0), (50, 20, 0)])
    send = cocotb.start_soon(_send_words(dut, [0x1111, 0x2222, 0x3333]))
    await _expect_status(dut, error=True, code=UnpackErrorCode.CONFIG)
    await send
    words = [_pack(_BYTE_SPECS, (9, 10, 11, 12))]
    assert await _run(dut, words, _BYTE_SPECS) == unpack_rows(words, _BYTE_SPECS)


@cocotb.test()
async def error_status_survives_backpressure(dut):
    # while status_ready is held low the error close-out must stay stable and
    # the tile must refuse new input; recovery only after the handshake
    await _start(dut)
    _drive_raw_config(dut, [(0, 8, 0), (8, 0, 0), (16, 8, 0), (24, 8, 0)])
    dut.status_ready.value = 0
    await _send_words(dut, [0x1234])  # single bad-config row

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
        assert int(dut.status_error_code.value) == UnpackErrorCode.CONFIG
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=True, code=UnpackErrorCode.CONFIG)
    words = [_pack(_BYTE_SPECS, (1, 2, 3, 4))]
    assert await _run(dut, words, _BYTE_SPECS) == unpack_rows(words, _BYTE_SPECS)


@cocotb.test()
async def error_close_out_waits_for_backpressured_tail(dut):
    # Cross-batch error ordering under output backpressure. Batch A (valid
    # config) forwards one row whose final beat is held under backpressure;
    # batch B follows immediately with an invalid config that would raise
    # ERR_CONFIG. The tile must not present B's error close-out on the status
    # stream until A's final output beat has handshaked -- otherwise B's
    # per-batch close-out overtakes A's still-in-flight tail (the lane status
    # mux serialises one close-out per batch, and the record writer's done
    # waits on it).
    await _start(dut)
    _drive_config(dut, _BYTE_SPECS)  # A: valid

    a_word = _pack(_BYTE_SPECS, (0x11, 0x22, 0x33, 0x44))
    expected = [(word, int(i == 1)) for i, word in enumerate(_expand(a_word, _BYTE_SPECS))]

    sender = cocotb.start_soon(_send_a_then_bad_b(dut, a_word))
    dut.status_ready.value = 1  # a downstream mux ready to accept a close-out

    words: list[tuple[int, int]] = []
    fires = 0
    a_tail_done = False
    holding = False
    hold = 0
    for _ in range(400):
        if fires < 1:
            ready = 1  # drain A's first word
        elif not holding and not a_tail_done:
            holding = True
            ready = 0
        elif holding and hold < 15:
            ready = 0
        else:
            ready = 1  # release: A's tail drains, then B's error may appear
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
            "batch B ERR_CONFIG close-out asserted (status_valid=1) before batch A's "
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

    # only now may B's error close-out appear; then the lane is quiescent
    await _expect_status(dut, error=True, code=UnpackErrorCode.CONFIG)
    await _assert_quiescent(dut)
    await sender

    # recovers cleanly
    good = [_pack(_BYTE_SPECS, (7, 8, 9, 10))]
    assert await _run(dut, good, _BYTE_SPECS) == unpack_rows(good, _BYTE_SPECS)


@cocotb.test()
async def idle_after_reset_is_quiescent(dut):
    # no input driven: neither the output nor the status stream may fire
    await _start(dut)
    await _assert_quiescent(dut)


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0x51EED)
    for trial in range(40):
        specs = _random_specs(rng)
        rows = []
        for _ in range(rng.randint(1, 10)):
            row = tuple(_random_field(rng, spec) for spec in specs)
            rows.append(row)
        words = [_pack(specs, row) for row in rows]
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):
            pattern = pattern + (1,)
        out = await _run(dut, words, specs, ready_pattern=pattern)
        assert out == unpack_rows(words, specs), f"trial {trial}: specs={specs} rows={rows}"


@cocotb.test()
async def bypass_passes_quad_rows_through_byte_identical(dut):
    # cfg_bypass=1: input is already 16-byte quad rows (two words each); the
    # tile forwards every word byte-identically, order/count preserved, last on
    # the batch's final word
    await _start(dut)
    dut.cfg_bypass.value = 1
    rows = [(0x11, 0x22, 0x33, 0x44), (568, -500, 6, 30), (-1, 2**31 - 1, 0, 255), (0, 0, 0, 0)]
    words = _bypass_words(rows)
    out = await _run_bypass(dut, words)
    assert out == words, "bypass output not byte-identical to input"
    # decoding the forwarded pairs recovers the quad rows (golden identity)
    decoded = []
    for offset in range(0, len(out), 2):
        w0, w1 = out[offset], out[offset + 1]
        decoded.append((_signed32(w0 & _MASK32), _signed32(w0 >> 32), _signed32(w1 & _MASK32), _signed32(w1 >> 32)))
    assert decoded == unpack_rows(rows, bypass=True)


@cocotb.test()
async def bypass_is_zero_added_latency(dut):
    # a true wire: the presented word appears on the output THIS cycle
    # (input_ready == output_ready, output_valid == input_valid), so the bypass
    # baseline is not taxed by a widen-then-renarrow register stage
    await _start(dut)
    dut.cfg_bypass.value = 1
    words = _bypass_words([(7, 8, 9, 10), (11, 12, 13, 14)])
    dut.output_ready.value = 1
    for index, word in enumerate(words):
        last = int(index == len(words) - 1)
        dut.input_data.value = word
        dut.input_last.value = last
        dut.input_valid.value = 1
        await ReadOnly()
        assert int(dut.input_ready.value) == 1, "bypass stalled with the downstream ready"
        assert int(dut.output_valid.value) == 1, "bypass added a latency cycle"
        assert int(dut.output_data.value) == word, "bypass output != input word in the same cycle"
        assert int(dut.output_last.value) == last, "bypass last not forwarded combinationally"
        await RisingEdge(dut.clk)
    dut.input_valid.value = 0
    dut.input_last.value = 0
    await _assert_quiescent(dut)


@cocotb.test()
async def bypass_mode_is_batch_latched(dut):
    # cfg_bypass latches at the first row like the field descriptors: flipping
    # it (and the descriptors) mid-batch must not re-slice the batch
    await _start(dut)
    dut.cfg_bypass.value = 1
    rows = [(0x11, 0x22, 0x33, 0x44), (568, -500, 6, 30)]
    words = _bypass_words(rows)
    send = cocotb.start_soon(_send_bypass_flip(dut, words))
    out = await _collect_output(dut, (1,), expect_words=len(words))
    await send
    assert out == words, "mid-batch cfg_bypass flip re-sliced a latched-bypass batch"


@cocotb.test()
async def bypass_backpressure_holds_stable(dut):
    # hold-stability in bypass mode: a stalled passthrough beat stays asserted
    # and byte-stable until it fires (the upstream holds it)
    await _start(dut)
    dut.cfg_bypass.value = 1
    rows = [(0x11, 0x22, 0x33, 0x44), (568, -500, 6, 30), (7, 8, 9, 10)]
    words = _bypass_words(rows)
    out = await _run_bypass(dut, words, ready_pattern=(1, 0, 0, 1, 0))
    assert out == words


@cocotb.test()
async def mode_switches_between_batches_on_one_dut(dut):
    # the whole point: ONE bitstream benchmarks packed (unpack) vs unpacked
    # (bypass) rows back to back on the same silicon
    await _start(dut)
    u_words = [_pack(_Q6_SPECS, row) for row in _Q6_ROWS]
    b_rows = [(9, 8, 7, 6), (5, 4, 3, 2)]
    b_words = _bypass_words(b_rows)
    for _ in range(2):
        dut.cfg_bypass.value = 0
        assert await _run(dut, u_words, _Q6_SPECS) == unpack_rows(u_words, _Q6_SPECS)
        dut.cfg_bypass.value = 1
        assert await _run_bypass(dut, b_words) == b_words


@cocotb.test()
async def bypass_waits_for_backpressured_unpack_tail(dut):
    # cross-mode emit-drain gate: an unpack batch A whose final beat is held
    # under backpressure, immediately followed by a bypass batch B. B's
    # passthrough must NOT drive the output (nor be accepted) until A's tail has
    # handshaked -- otherwise B overtakes A's still-in-flight tail across the
    # mode change.
    await _start(dut)
    a_word = _pack(_BYTE_SPECS, (0x11, 0x22, 0x33, 0x44))
    a_out = _expand(a_word, _BYTE_SPECS)  # [w0, w1]; w1 is the last beat
    b_words = _bypass_words([(7, 8, 9, 10), (11, 12, 13, 14)])

    sender = cocotb.start_soon(_send_unpack_then_bypass(dut, a_word, b_words))

    collected: list[tuple[int, int]] = []
    fires = 0
    a_tail_done = False
    holding = False
    hold = 0
    for _ in range(400):
        if fires < 1:
            ready = 1  # drain A's first word
        elif not holding and not a_tail_done:
            holding = True
            ready = 0
        elif holding and hold < 15:
            ready = 0
        else:
            ready = 1  # release: A's tail drains, then B may forward
        dut.output_ready.value = ready

        await ReadOnly()
        valid = int(dut.output_valid.value) == 1
        last = int(dut.output_last.value) == 1
        word = int(dut.output_data.value)
        in_ready = int(dut.input_ready.value) == 1
        fired = ready == 1 and valid
        # THE PROPERTY: until A's tail handshakes, the output can only carry A's
        # stalled tail (never B's word) and B must not be accepted.
        if fires >= 1 and not a_tail_done:
            assert valid and word == a_out[1] and last, "output not holding A's stalled tail before it drained"
            assert not in_ready, "bypass B accepted while A's tail still in flight (cross-mode overtake)"
        await RisingEdge(dut.clk)
        if holding:
            hold += 1
        if fired:
            collected.append((word, last))
            fires += 1
            if last:
                a_tail_done = True
                break

    assert a_tail_done, "A's tail never handshaked"
    assert collected == [(a_out[0], False), (a_out[1], True)], f"A's beats wrong: {collected}"

    # only now may B's bypass words flow, byte-identical, last on B's final word
    b_out = await _collect_output(dut, (1,), expect_words=len(b_words))
    assert b_out == b_words, "bypass B not forwarded byte-identically after A drained"
    await sender


@cocotb.test()
async def bypass_torn_row_reports_stream_error(dut):
    # a bypass batch ending on a w0 (odd word count) is a torn quad row: the
    # torn w0 is never forwarded (nor tagged last) and the batch is invalidated
    # with ERR_STREAM -- the quad-row-tile convention (input_last legal only on
    # a w1)
    await _start(dut)
    dut.cfg_bypass.value = 1
    good = _quad_words((0x11, 0x22, 0x33, 0x44))  # one complete row (w0, w1)
    torn = good + [0x1234_5678_9ABC_DEF0]  # + a lone w0 carrying last
    send = cocotb.start_soon(_send_words(dut, torn))
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    forwarded: list[tuple[int, int]] = []
    raised = False
    for _ in range(200):
        await ReadOnly()
        if int(dut.output_ready.value) == 1 and int(dut.output_valid.value) == 1:
            forwarded.append((int(dut.output_data.value), int(dut.output_last.value)))
        got_status = int(dut.status_valid.value) == 1
        err = int(dut.status_error.value)
        code = int(dut.status_error_code.value)
        await RisingEdge(dut.clk)
        if got_status:
            assert err == 1 and code == UnpackErrorCode.STREAM, f"torn bypass status {err}/{code}"
            raised = True
            break
    assert raised, "no ERR_STREAM close-out for a torn bypass batch"
    await send
    # the complete row's two words were forwarded, neither tagged last; the
    # torn w0 was dropped, not emitted
    assert forwarded == [(good[0], 0), (good[1], 0)], forwarded
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    # recovers cleanly with a well-framed bypass batch
    rows = [(7, 8, 9, 10)]
    assert await _run_bypass(dut, _bypass_words(rows)) == _bypass_words(rows)


@cocotb.test()
async def bypass_torn_row_reports_error_under_backpressure(dut):
    # the torn w0 is dropped, not forwarded, so its acceptance cannot depend on
    # output_ready: ERR_STREAM must still fire with the sink never ready (no
    # deadlock, and no output beat leaks)
    await _start(dut)
    dut.cfg_bypass.value = 1
    dut.output_ready.value = 0  # sink never asserts ready
    dut.status_ready.value = 1
    send = cocotb.start_soon(_send_words(dut, [0x0BAD_0BAD_0BAD_0BAD]))  # lone w0 with last
    raised = False
    for _ in range(64):
        await ReadOnly()
        assert int(dut.output_valid.value) == 0, "torn w0 forwarded despite backpressure"
        got_status = int(dut.status_valid.value) == 1
        err = int(dut.status_error.value)
        code = int(dut.status_error_code.value)
        await RisingEdge(dut.clk)
        if got_status:
            assert err == 1 and code == UnpackErrorCode.STREAM, f"torn bypass status {err}/{code}"
            raised = True
            break
    assert raised, "torn bypass batch deadlocked under output backpressure"
    await send
    dut.status_ready.value = 0
    rows = [(7, 8, 9, 10)]
    assert await _run_bypass(dut, _bypass_words(rows)) == _bypass_words(rows)


# ---- helpers ----


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _int_in_width(width: int) -> int:
    # the most-negative signed value at this width
    return -(1 << (width - 1))


def _pack(specs, fields) -> int:
    word = 0
    for spec, value in zip(specs, fields):
        word |= (value & ((1 << spec.width) - 1)) << spec.offset
    return word


def _expand(word: int, specs) -> list[int]:
    """The two output words the tile emits for one packed word."""
    key, value, aux0, aux1 = unpack_rows([word], specs)[0]
    return [((value & _MASK32) << 32) | (key & _MASK32), ((aux1 & _MASK32) << 32) | (aux0 & _MASK32)]


def _quad_words(row) -> list[int]:
    """A 16-byte quad row as its two 64-bit stream words (w0 = {value, key},
    w1 = {aux1, aux0}) — the bypass-mode input framing (two words per row)."""
    key, value, aux0, aux1 = row
    return [((value & _MASK32) << 32) | (key & _MASK32), ((aux1 & _MASK32) << 32) | (aux0 & _MASK32)]


def _bypass_words(rows) -> list[int]:
    """The bypass-mode input stream for a batch of quad rows: two words each."""
    words: list[int] = []
    for row in rows:
        words.extend(_quad_words(row))
    return words


async def _run_bypass(dut, words, *, ready_pattern=(1,)) -> list[int]:
    """Drive a bypass batch (cfg_bypass must already be 1) and collect the
    forwarded stream. Bypass is 1:1 word-for-word (no widening), so the output
    is exactly ``len(words)`` words; _send_words/_collect_output already assert
    hold-stability, last placement, and silent-success quiescence."""
    send = cocotb.start_soon(_send_words(dut, words))
    collected = await _collect_output(dut, ready_pattern, expect_words=len(words))
    await send
    return collected


def _drive_config(dut, specs) -> None:
    for i, spec in enumerate(specs):
        getattr(dut, f"cfg_field{i}_offset").value = spec.offset
        getattr(dut, f"cfg_field{i}_width").value = spec.width
        getattr(dut, f"cfg_field{i}_signed").value = int(spec.signed)


def _drive_raw_config(dut, descriptors) -> None:
    for i, (offset, width, signed) in enumerate(descriptors):
        getattr(dut, f"cfg_field{i}_offset").value = offset
        getattr(dut, f"cfg_field{i}_width").value = width
        getattr(dut, f"cfg_field{i}_signed").value = int(signed)


def _random_specs(rng):
    # four non-overlapping fields packed left to right, total <= 64 bits
    specs = []
    offset = 0
    for _ in range(4):
        remaining = 64 - offset  # bits left for this and following fields
        # leave at least one bit for each later field
        max_width = max(1, min(32, remaining - (3 - len(specs))))
        width = rng.randint(1, max_width)
        specs.append(UnpackFieldSpec(offset=offset, width=width, signed=bool(rng.getrandbits(1))))
        offset += width
    return specs


def _random_field(rng, spec: UnpackFieldSpec) -> int:
    if spec.signed:
        lo, hi = -(1 << (spec.width - 1)), (1 << (spec.width - 1)) - 1
    else:
        lo, hi = 0, (1 << spec.width) - 1
    if rng.random() < 0.2:
        return rng.choice((lo, hi, 0))
    return rng.randint(lo, hi)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    _drive_config(dut, _BYTE_SPECS)
    dut.cfg_bypass.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, words, specs, *, ready_pattern=(1,)):
    _drive_config(dut, specs)
    send = cocotb.start_soon(_send_words(dut, words))
    collected = await _collect_output(dut, ready_pattern, expect_words=2 * len(words))
    await send
    out = []
    for offset in range(0, len(collected), 2):
        w0, w1 = collected[offset], collected[offset + 1]
        out.append((_signed32(w0 & _MASK32), _signed32(w0 >> 32), _signed32(w1 & _MASK32), _signed32(w1 >> 32)))
    return out


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


async def _send_bypass_flip(dut, words) -> None:
    # send a bypass batch, flipping cfg_bypass (and the field descriptors) to an
    # unpack config after the first word fires; the batch-latched mode must
    # ignore the flip and stay a byte-identical passthrough for the whole batch
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
            dut.cfg_bypass.value = 0
            _drive_config(dut, _BYTE_SPECS)
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _send_unpack_then_bypass(dut, a_word, b_words) -> None:
    # A: a complete one-row unpack batch (last); then batch B: a bypass batch.
    # B re-latches the mode at its first row, so it must be held off until A's
    # in-flight tail has drained.
    dut.cfg_bypass.value = 0
    dut.input_data.value = a_word
    dut.input_last.value = 1
    dut.input_valid.value = 1
    while True:
        await ReadOnly()
        fired = int(dut.input_ready.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            break
    dut.cfg_bypass.value = 1  # B: bypass
    await _send_words(dut, b_words)


async def _send_a_then_bad_b(dut, a_word) -> None:
    # A: a complete one-row batch (last); then batch B: one bad-config row
    # (last). B is a separate batch, so its first row re-checks the config.
    dut.input_data.value = a_word
    dut.input_last.value = 1
    dut.input_valid.value = 1
    while True:
        await ReadOnly()
        fired = int(dut.input_ready.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            break
    # A's batch has closed; retarget the cfg ports to an invalid descriptor for B
    _drive_raw_config(dut, [(0, 8, 0), (8, 0, 0), (16, 8, 0), (24, 8, 0)])
    dut.input_data.value = 0xBADB
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


async def _collect_output(dut, ready_pattern, *, expect_words: int) -> list[int]:
    words: list[int] = []
    pattern_index = 0
    held = None
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
                # a successful batch is silent: the downstream tile closes out
                await _assert_quiescent(dut)
                return words
    raise AssertionError(f"timed out collecting unpack output ({len(words)}/{expect_words} words)")


async def _assert_quiescent(dut) -> None:
    """Nothing may appear on the output or status streams even with ready held
    high (silent-success contract)."""
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    for _ in range(6):
        await ReadOnly()
        assert int(dut.output_valid.value) == 0, "output beat with no batch in flight"
        assert int(dut.status_valid.value) == 0, "status beat with no batch in flight"
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
    raise AssertionError("timed out waiting for unpack status")

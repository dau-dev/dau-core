from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import UnpackErrorCode, UnpackFieldSpec, unpack_rows

# OUT_WIDTH == 128 (the rows/cycle axis): the same tile with a 128-bit quad
# stream — one whole quad row per output beat ({w1, w0}), lifting the shared
# 2-beat emit cap to 1 row/cycle. Runner passes parameters={"OUT_WIDTH": 128};
# the width-agnostic helpers are shared with the 64-bit bench.
from dau_core.tests.cocotb_benches.dau_row_unpack_tb import (
    _BYTE_SPECS,
    _MASK32,
    _Q6_ROWS,
    _Q6_SPECS,
    _bypass_words,
    _drive_config,
    _drive_raw_config,
    _expect_status,
    _pack,
    _random_field,
    _random_specs,
    _send_words,
    _signed32,
)
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest


def _decode_beat(beat: int) -> tuple[int, int, int, int]:
    """One 128-bit output beat back into the (key, value, aux0, aux1) quad
    row: bits [63:0] = w0 = {value, key}, [127:64] = w1 = {aux1, aux0}."""
    return (
        _signed32(beat & _MASK32),
        _signed32((beat >> 32) & _MASK32),
        _signed32((beat >> 64) & _MASK32),
        _signed32((beat >> 96) & _MASK32),
    )


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


async def _collect_beats(dut, ready_pattern, *, expect_beats: int) -> list[int]:
    """Collect whole-row beats with the valid/ready hold rule asserted and the
    silent-success quiescence check after the batch's last beat."""
    beats: list[int] = []
    held = None
    for pattern_index in range(200000):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        dut.output_ready.value = ready
        await ReadOnly()
        valid = int(dut.output_valid.value) == 1
        fired = ready == 1 and valid
        beat = int(dut.output_data.value)
        last = int(dut.output_last.value)
        if held is not None:
            assert valid, "output_valid dropped while a beat was stalled"
            assert (beat, last) == held, "output beat changed while stalled"
        held = None if (fired or not valid) else (beat, last)
        await RisingEdge(dut.clk)
        if fired:
            beats.append(beat)
            if last == 1:
                dut.output_ready.value = 0
                assert len(beats) == expect_beats, f"expected {expect_beats} beats, got {len(beats)}"
                await _assert_quiescent(dut)
                return beats
    raise AssertionError(f"timed out collecting wide unpack output ({len(beats)}/{expect_beats} beats)")


async def _assert_quiescent(dut) -> None:
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    for _ in range(6):
        await ReadOnly()
        assert int(dut.output_valid.value) == 0, "output beat with no batch in flight"
        assert int(dut.status_valid.value) == 0, "status beat with no batch in flight"
        await RisingEdge(dut.clk)
    dut.output_ready.value = 0
    dut.status_ready.value = 0


async def _run(dut, words, specs, *, ready_pattern=(1,)):
    _drive_config(dut, specs)
    send = cocotb.start_soon(_send_words(dut, words))
    beats = await _collect_beats(dut, ready_pattern, expect_beats=len(words))
    await send
    return [_decode_beat(beat) for beat in beats]


async def _run_bypass(dut, words, *, ready_pattern=(1,)):
    """Drive a bypass batch (two words per row) and collect the gearboxed
    whole-row beats: len(words)/2 beats out."""
    send = cocotb.start_soon(_send_words(dut, words))
    beats = await _collect_beats(dut, ready_pattern, expect_beats=len(words) // 2)
    await send
    return [_decode_beat(beat) for beat in beats]


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    _drive_config(dut, _BYTE_SPECS)
    words = list(range(256))
    await assert_sustained_ingest(
        dut,
        "row-unpack",
        send=lambda: _send_words(dut, words),
        input_beats=len(words),
        params={"OUT_WIDTH": 128},
    )


@cocotb.test()
async def wide_unpack_emits_one_quad_row_per_beat(dut):
    await _start(dut)
    words = [_pack(_Q6_SPECS, row) for row in _Q6_ROWS]
    out = await _run(dut, words, _Q6_SPECS)
    assert out == unpack_rows(words, _Q6_SPECS)
    assert out[0] == (568, 1000, 5, 10)


@cocotb.test()
async def wide_sustains_one_row_per_cycle(dut):
    # THE AXIS PROPERTY: with the sink always ready, N packed words stream
    # through in ~N cycles — not the 2N of the 64-bit two-beat emit. This is
    # the shared-front cap the parameter exists to lift.
    await _start(dut)
    rows = [(i & 0x3FF, i * 3, i & 0x7F, i & 0x3F) for i in range(64)]
    # simple disjoint layout wide enough for the generated values (offsets
    # 0/10/42/55 mirror the q1-like packing)
    specs = [
        UnpackFieldSpec(offset=0, width=10),
        UnpackFieldSpec(offset=10, width=32),
        UnpackFieldSpec(offset=42, width=13),
        UnpackFieldSpec(offset=55, width=8),
    ]
    words = [_pack(specs, row) for row in rows]
    _drive_config(dut, specs)
    dut.output_ready.value = 1
    send = cocotb.start_soon(_send_words(dut, words))
    cycles = 0
    fired = 0
    started = False
    out: list[int] = []
    for _ in range(4 * len(words) + 32):
        await ReadOnly()
        if int(dut.output_valid.value) == 1:
            started = True
            out.append(int(dut.output_data.value))
            fired += 1
        if started:
            cycles += 1
        await RisingEdge(dut.clk)
        if fired == len(words):
            break
    await send
    dut.output_ready.value = 0
    assert fired == len(words), f"only {fired}/{len(words)} beats fired"
    assert [_decode_beat(beat) for beat in out] == unpack_rows(words, specs)
    # sustained 1 row/cycle: the fired-beat window is ~N cycles, far under 2N
    assert cycles <= len(words) + 4, f"wide emit took {cycles} cycles for {len(words)} rows (2-beat serialization not lifted)"


@cocotb.test()
async def wide_backpressure_holds_stable(dut):
    await _start(dut)
    words = [_pack(_Q6_SPECS, row) for row in _Q6_ROWS]
    out = await _run(dut, words, _Q6_SPECS, ready_pattern=(1, 0, 0, 1, 0))
    assert out == unpack_rows(words, _Q6_SPECS)


@cocotb.test()
async def wide_bypass_gearboxes_two_words_into_one_beat(dut):
    # bypass input stays the two-word quad framing; the wide tile assembles
    # each row into ONE {w1, w0} beat (golden identity, order/count preserved)
    await _start(dut)
    dut.cfg_bypass.value = 1
    rows = [(0x11, 0x22, 0x33, 0x44), (568, -500, 6, 30), (-1, 2**31 - 1, 0, 255), (0, 0, 0, 0)]
    out = await _run_bypass(dut, _bypass_words(rows))
    assert out == unpack_rows(rows, bypass=True)


@cocotb.test()
async def wide_bypass_backpressure_holds_stable(dut):
    await _start(dut)
    dut.cfg_bypass.value = 1
    rows = [(7, 8, 9, 10), (11, 12, 13, 14), (568, -500, 6, 30)]
    out = await _run_bypass(dut, _bypass_words(rows), ready_pattern=(1, 0, 0, 1, 0))
    assert out == unpack_rows(rows, bypass=True)


@cocotb.test()
async def wide_bypass_torn_row_reports_stream_error(dut):
    # a bypass batch ending on a w0 is a torn quad row: nothing is forwarded
    # for it (the incomplete row never reaches the output) and the batch is
    # invalidated with ERR_STREAM
    await _start(dut)
    dut.cfg_bypass.value = 1
    good = _bypass_words([(0x11, 0x22, 0x33, 0x44)])
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
    assert raised, "no ERR_STREAM close-out for a torn wide-bypass batch"
    await send
    # exactly the complete row's single gearboxed beat, not tagged last
    assert [(_decode_beat(beat), last) for beat, last in forwarded] == [((0x11, 0x22, 0x33, 0x44), 0)], forwarded
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    rows = [(7, 8, 9, 10)]
    assert await _run_bypass(dut, _bypass_words(rows)) == unpack_rows(rows, bypass=True)


@cocotb.test()
async def wide_bad_config_drains_then_config_error(dut):
    await _start(dut)
    _drive_raw_config(dut, [(0, 8, 0), (8, 0, 0), (16, 8, 0), (24, 8, 0)])  # width 0 invalid
    send = cocotb.start_soon(_send_words(dut, [0x1234_5678, 0x9ABC_DEF0]))
    await _expect_status(dut, error=True, code=UnpackErrorCode.CONFIG)
    await send
    words = [_pack(_BYTE_SPECS, (1, 2, 3, 4))]
    assert await _run(dut, words, _BYTE_SPECS) == unpack_rows(words, _BYTE_SPECS)


@cocotb.test()
async def wide_bypass_torn_row_reports_error_under_backpressure(dut):
    # the torn w0 (last at phase 0) is accepted-and-dropped, never forwarded,
    # so its acceptance cannot depend on output_ready: ERR_STREAM must still
    # fire with the sink NEVER ready (no deadlock), and no output beat leaks
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
    assert raised, "torn wide-bypass batch deadlocked under output backpressure"
    await send
    dut.status_ready.value = 0
    rows = [(7, 8, 9, 10)]
    assert await _run_bypass(dut, _bypass_words(rows)) == unpack_rows(rows, bypass=True)


async def _send_one_unpack_row(dut, word) -> None:
    """Present one complete unpack batch (a single packed word carrying last)
    and wait for it to be accepted."""
    dut.input_data.value = word
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


@cocotb.test()
async def wide_bypass_waits_for_backpressured_unpack_tail(dut):
    # cross-mode emit-drain gate in WIDE: unpack batch A's single beat is held
    # under backpressure while bypass batch B is presented. B must NOT be
    # accepted (nor drive the output) until A's beat has handshaked — the
    # overtake gate must hold when the whole row is one beat.
    await _start(dut)
    dut.cfg_bypass.value = 0
    a_word = _pack(_BYTE_SPECS, (0x11, 0x22, 0x33, 0x44))
    a_row = unpack_rows([a_word], _BYTE_SPECS)[0]
    dut.output_ready.value = 0  # stall A's beat before it can fire
    await _send_one_unpack_row(dut, a_word)

    # present B (bypass) while A's beat is in flight
    dut.cfg_bypass.value = 1
    b_words = _bypass_words([(7, 8, 9, 10)])
    dut.input_data.value = b_words[0]
    dut.input_last.value = 0
    dut.input_valid.value = 1
    for _ in range(12):
        await ReadOnly()
        assert int(dut.input_ready.value) == 0, "bypass B accepted while A's wide beat still in flight"
        assert int(dut.output_valid.value) == 1, "A's stalled beat dropped"
        assert _decode_beat(int(dut.output_data.value)) == a_row, "output not holding A's beat"
        assert int(dut.output_last.value) == 1, "A's single beat must carry last"
        await RisingEdge(dut.clk)

    # withdraw the probe (B was only presented to prove the gate), release the
    # stall so A's beat drains, then send B properly and expect its gearboxed row
    dut.input_valid.value = 0
    dut.output_ready.value = 1
    await ReadOnly()
    assert int(dut.output_valid.value) == 1 and int(dut.output_last.value) == 1
    await RisingEdge(dut.clk)
    dut.output_ready.value = 0
    assert await _run_bypass(dut, b_words) == unpack_rows([(7, 8, 9, 10)], bypass=True)


@cocotb.test()
async def wide_error_close_out_waits_for_backpressured_tail(dut):
    # cross-batch status-overtake gate in WIDE (exercises S_ERR_DRAIN): batch
    # A's single wide beat is stalled; batch B (bad config, single row) is
    # presented. B is only accepted on the cycle A's beat handshakes; its
    # ERR_CONFIG close-out must NOT appear before A's beat has drained, and
    # must be reported through S_ERR_DRAIN afterwards.
    await _start(dut)
    a_word = _pack(_BYTE_SPECS, (0x51, 0x52, 0x53, 0x54))
    a_row = unpack_rows([a_word], _BYTE_SPECS)[0]
    dut.output_ready.value = 0
    dut.status_ready.value = 1
    await _send_one_unpack_row(dut, a_word)

    # present B: a single bad-config row carrying last (width 0 invalid)
    _drive_raw_config(dut, [(0, 8, 0), (8, 0, 0), (16, 8, 0), (24, 8, 0)])
    dut.input_data.value = 0xBADB
    dut.input_last.value = 1
    dut.input_valid.value = 1

    # while A's beat is stalled: no status may appear (overtake gate)
    for _ in range(12):
        await ReadOnly()
        assert int(dut.status_valid.value) == 0, "B's ERR_CONFIG overtook A's un-drained beat"
        assert int(dut.output_valid.value) == 1, "A's stalled beat dropped"
        assert _decode_beat(int(dut.output_data.value)) == a_row
        await RisingEdge(dut.clk)

    # release: A's beat and B's acceptance handshake on the SAME cycle — the
    # unpack accept gate is emit_free, which is exactly the final beat's fire —
    # so capture both handshakes together and deassert B immediately after.
    # This pins exactly ONE B transaction, taken while the emit engine was
    # busy (the S_ERR_DRAIN path), never a replay from idle.
    dut.output_ready.value = 1
    a_drained = False
    b_accepted = False
    for _ in range(64):
        await ReadOnly()
        out_fire = int(dut.output_valid.value) == 1 and int(dut.output_ready.value) == 1
        in_fire = int(dut.input_valid.value) == 1 and int(dut.input_ready.value) == 1
        if not a_drained and int(dut.status_valid.value) == 1:
            raise AssertionError("ERR_CONFIG appeared before A's beat drained")
        await RisingEdge(dut.clk)
        if out_fire:
            a_drained = True
            b_accepted = in_fire
            break
    assert a_drained, "A's beat never drained"
    assert b_accepted, "B was not accepted on A's drain cycle (the emit_free accept)"
    dut.input_valid.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    # the S_ERR_DRAIN cycle: input held closed, no status yet — the close-out
    # is deferred through the drain state, not raised from idle
    await ReadOnly()
    assert int(dut.input_ready.value) == 0, "input open during S_ERR_DRAIN"
    assert int(dut.status_valid.value) == 0, "status raised before the drain cycle elapsed"
    await RisingEdge(dut.clk)
    await _expect_status(dut, error=True, code=UnpackErrorCode.CONFIG)
    # recovers cleanly
    good = [_pack(_BYTE_SPECS, (1, 2, 3, 4))]
    assert await _run(dut, good, _BYTE_SPECS) == unpack_rows(good, _BYTE_SPECS)


@cocotb.test()
async def wide_mode_switches_between_batches_on_one_dut(dut):
    await _start(dut)
    u_words = [_pack(_Q6_SPECS, row) for row in _Q6_ROWS]
    b_rows = [(9, 8, 7, 6), (5, 4, 3, 2)]
    for _ in range(2):
        dut.cfg_bypass.value = 0
        assert await _run(dut, u_words, _Q6_SPECS) == unpack_rows(u_words, _Q6_SPECS)
        dut.cfg_bypass.value = 1
        assert await _run_bypass(dut, _bypass_words(b_rows)) == unpack_rows(b_rows, bypass=True)


@cocotb.test()
async def wide_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0xD1CE)
    for trial in range(40):
        specs = _random_specs(rng)
        rows = [tuple(_random_field(rng, spec) for spec in specs) for _ in range(rng.randint(1, 10))]
        words = [_pack(specs, row) for row in rows]
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):
            pattern = pattern + (1,)
        out = await _run(dut, words, specs, ready_pattern=pattern)
        assert out == unpack_rows(words, specs), f"trial {trial}: specs={specs} rows={rows}"

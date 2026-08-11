from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import UnpackErrorCode, UnpackFieldSpec, unpack_rows
from dau_core.tests.cocotb_benches.dau_int32_row_unpack_tb import (
    _MASK32,
    _Q6_SPECS,
    _drive_config,
    _pack,
    _random_field,
    _random_specs,
    _signed32,
)
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

# Generic over SLOTS = OUT_WIDTH/128, the rows-per-beat axis. The directed
# 64- and 128-bit benches pin the q6/q1 field maps and the bypass framing;
# this one proves the MULTI-ROW BEAT actually works rather than merely
# elaborating -- that slot s reads packed row s and lands at quad row s.
#
# The failure this is built to catch: a slice stride hardcoded to one row, so
# every slot reads packed row 0 and the tile emits SLOTS copies of it. Every
# row therefore carries DISTINCT field values, and no test uses a uniform
# batch, because a uniform batch cannot tell a correct stride from a broken
# one.
SLOTS = int(os.environ.get("DAU_UNPACK_SLOTS", "2"))
OUT_WIDTH = SLOTS * 128
IN_WIDTH = SLOTS * 64

PAD_PACKED = (1 << 64) - 1
PAD_ROW = (1 << 128) - 1


@cocotb.test()
async def multi_slot_unpack_matches_golden(dut):
    await _start(dut)
    rng = random.Random(20260811)
    rows = [tuple(_random_field(rng, spec) for spec in _Q6_SPECS) for _ in range(8 * SLOTS)]
    words = [_pack(_Q6_SPECS, row) for row in rows]
    assert await _run(dut, words, _Q6_SPECS) == unpack_rows(words, _Q6_SPECS)


@cocotb.test()
async def every_slot_reads_its_own_packed_row(dut):
    """The stride test, stated directly.

    One beat, every slot carrying a different key. A tile whose slice stride
    is hardcoded emits SLOTS copies of slot 0 and passes any test built on a
    uniform batch; here the keys are all distinct, so it cannot.
    """
    await _start(dut)
    rows = [(600 + index, 1000 * (index + 1), index, index + 1) for index in range(SLOTS)]
    words = [_pack(_Q6_SPECS, row) for row in rows]
    out = await _run(dut, words, _Q6_SPECS)
    assert out == unpack_rows(words, _Q6_SPECS)
    assert len({row[0] for row in out}) == SLOTS, f"slots are not distinct: {out}"


@cocotb.test()
async def random_field_maps_match_golden(dut):
    """Sweep field maps, not just the q6 one: an offset/width/sign combination
    is what a hardcoded slice gets wrong at a width other than the one it was
    written against."""
    rng = random.Random(7)
    for _ in range(6):
        await _start(dut)
        specs = _packable_specs(rng)
        rows = [tuple(_random_field(rng, spec) for spec in specs) for _ in range(3 * SLOTS)]
        words = [_pack(specs, row) for row in rows]
        assert await _run(dut, words, specs) == unpack_rows(words, specs)


@cocotb.test()
async def ragged_tail_pads_pass_through_unsliced(dut):
    """A row count that is not a multiple of SLOTS is padded by the host with
    the reserved all-ones packed word. It must emerge as the reserved all-ones
    PAD quad row -- NOT as whatever the field descriptors would slice out of
    it -- so the wide-row tiles downstream drop it by the convention they
    already implement."""
    if SLOTS == 1:
        return  # raggedness cannot arise when a beat is exactly one row
    await _start(dut)
    rng = random.Random(99)
    real = [tuple(_random_field(rng, spec) for spec in _Q6_SPECS) for _ in range(2 * SLOTS + 1)]
    words = [_pack(_Q6_SPECS, row) for row in real]
    pads = (-len(words)) % SLOTS
    assert pads, "pick a row count that is genuinely ragged"
    beats = _beats(words + [PAD_PACKED] * pads)

    raw = await _run_raw(dut, beats, _Q6_SPECS)
    assert raw[-pads:] == [PAD_ROW] * pads, "a pad was sliced instead of passed through"
    assert [_decode_row(row) for row in raw[:-pads]] == unpack_rows(words, _Q6_SPECS)


@cocotb.test()
async def backpressure_preserves_rows_and_order(dut):
    await _start(dut)
    rng = random.Random(31337)
    rows = [tuple(_random_field(rng, spec) for spec in _Q6_SPECS) for _ in range(6 * SLOTS)]
    words = [_pack(_Q6_SPECS, row) for row in rows]
    assert await _run(dut, words, _Q6_SPECS, ready_pattern=(1, 0, 0, 1, 0)) == unpack_rows(words, _Q6_SPECS)


@cocotb.test()
async def output_last_lands_on_the_final_beat(dut):
    await _start(dut)
    rng = random.Random(4242)
    rows = [tuple(_random_field(rng, spec) for spec in _Q6_SPECS) for _ in range(4 * SLOTS)]
    words = [_pack(_Q6_SPECS, row) for row in rows]
    _drive_config(dut, _Q6_SPECS)
    send = cocotb.start_soon(_send_beats(dut, _beats(words)))
    beats, last_at = await _collect_with_last(dut)
    await send
    assert last_at == len(beats) - 1, (last_at, len(beats))
    assert len(beats) == len(words) // SLOTS


@cocotb.test()
async def sustained_ingest_meets_the_slots_floor(dut):
    """SLOTS rows per beat is the whole point of the parameter, so the rate is
    asserted against the registry declaration rather than assumed."""
    await _start(dut)
    rng = random.Random(5)
    # the probe requires >= 256 rows for the measurement to mean anything; 512
    # is a whole number of beats at every SLOTS in the sweep
    rows = [tuple(_random_field(rng, spec) for spec in _Q6_SPECS) for _ in range(512)]
    words = [_pack(_Q6_SPECS, row) for row in rows]
    beats = _beats(words)
    _drive_config(dut, _Q6_SPECS)
    measured = await assert_sustained_ingest(
        dut,
        "int32-row-unpack",
        send=lambda: _send_beats(dut, beats),
        input_beats=len(beats),
        rows_per_beat=SLOTS,
        params={"OUT_WIDTH": OUT_WIDTH},
    )
    # The registry floor is DELIBERATELY capped at 1 row/cycle until silicon
    # proves the multi-row claim, so clearing it proves nothing about SLOTS:
    # at SLOTS=8 a tile accepting one beat every eight cycles still measures
    # 8/8 = 1 and would pass. Assert the rate this parameter exists to buy.
    assert measured >= SLOTS, f"SLOTS={SLOTS} must sustain {SLOTS} rows/cycle, measured {measured}"



def _packable_specs(rng):
    """A random field map this tile will accept at the configured SLOTS.

    ``_random_specs`` happily fills all 64 bits, and at SLOTS > 1 a layout
    with no spare bit is refused as a bad config precisely because it could
    forge the all-ones PAD. Draw again until there is headroom rather than
    weakening the constraint -- the rejection is the point.
    """
    for _ in range(64):
        specs = _random_specs(rng)
        if SLOTS == 1 or sum(spec.width for spec in specs) < 64:
            return specs
    raise AssertionError("no layout with a spare bit drawn in 64 attempts")


def _beats(words: list[int]) -> list[int]:
    """Pack SLOTS packed rows into one input beat, row s at [64*s +: 64]."""
    assert len(words) % SLOTS == 0, "caller pads to a whole beat"
    return [
        sum(words[offset + slot] << (64 * slot) for slot in range(SLOTS))
        for offset in range(0, len(words), SLOTS)
    ]


def _decode_row(row: int) -> tuple[int, int, int, int]:
    """One 128-bit quad row: [63:0] = w0 = {value, key}, [127:64] = w1 = {aux1, aux0}."""
    return (
        _signed32(row & _MASK32),
        _signed32((row >> 32) & _MASK32),
        _signed32((row >> 64) & _MASK32),
        _signed32((row >> 96) & _MASK32),
    )


def _split_beat(beat: int) -> list[int]:
    return [(beat >> (128 * slot)) & ((1 << 128) - 1) for slot in range(SLOTS)]


async def _start(dut) -> None:
    # A clock PER TEST is correct here, not a shared one: cocotb cancels every
    # coroutine started with start_soon when a test ends, so a single clock
    # started in the first test leaves later tests with no clock at all and
    # simulation time simply stops. Within one test _start is called once.
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start(start_high=False))
    dut.rst.value = 1
    _drive_config(dut, _Q6_SPECS)
    dut.cfg_bypass.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run_raw(dut, beats, specs, *, ready_pattern=(1,)) -> list[int]:
    """The emitted quad rows, still undecoded, so a pad stays visible."""
    _drive_config(dut, specs)
    send = cocotb.start_soon(_send_beats(dut, beats))
    collected = await _collect_output(dut, ready_pattern, expect_beats=len(beats))
    await send
    return [row for beat in collected for row in _split_beat(beat)]


async def _run(dut, words, specs, *, ready_pattern=(1,)):
    raw = await _run_raw(dut, _beats(words), specs, ready_pattern=ready_pattern)
    return [_decode_row(row) for row in raw]


async def _send_beats(dut, beats) -> None:
    for index, beat in enumerate(beats):
        dut.input_data.value = beat
        dut.input_last.value = int(index == len(beats) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _collect_output(dut, ready_pattern, *, expect_beats: int) -> list[int]:
    beats: list[int] = []
    for index in range(8192):
        dut.output_ready.value = ready_pattern[index % len(ready_pattern)]
        await ReadOnly()
        fired = int(dut.output_ready.value) == 1 and int(dut.output_valid.value) == 1
        value = int(dut.output_data.value)
        await RisingEdge(dut.clk)
        if fired:
            beats.append(value)
            if len(beats) == expect_beats:
                dut.output_ready.value = 0
                return beats
    raise AssertionError(f"timed out after {len(beats)} of {expect_beats} beats")


async def _collect_with_last(dut) -> tuple[list[int], int | None]:
    beats: list[int] = []
    last_at = None
    for _ in range(8192):
        dut.output_ready.value = 1
        await ReadOnly()
        fired = int(dut.output_valid.value) == 1
        value = int(dut.output_data.value)
        last = int(dut.output_last.value)
        await RisingEdge(dut.clk)
        if fired:
            beats.append(value)
            if last == 1:
                last_at = len(beats) - 1
                dut.output_ready.value = 0
                break
    return beats, last_at


@cocotb.test()
async def bypass_short_final_beat_is_not_torn(dut):
    """REGRESSION. At SLOTS == 1 an input beat is half a quad row, so
    input_last on phase 0 really is a torn row. At SLOTS > 1 the beat holds
    SLOTS/2 WHOLE quad rows, and a batch with an odd number of rows ends
    exactly there -- rejecting it as torn refuses a well-formed stream.

    The generalization inherited the SLOTS == 1 rule unchanged, so this
    passed review and simulation until an independent reader asked what
    phase 0 MEANS when a beat carries whole rows.
    """
    if SLOTS == 1:
        return  # phase-0 last genuinely is a tear here
    await _start(dut)
    dut.cfg_bypass.value = 1
    rows_per_beat = SLOTS // 2
    quads = [((0xA0 + i) << 96) | ((0xB0 + i) << 64) | ((0xC0 + i) << 32) | (0xD0 + i) for i in range(rows_per_beat)]
    beat = sum(q << (128 * i) for i, q in enumerate(quads))
    # ONE input beat, last asserted on phase 0
    dut.input_data.value = beat & ((1 << IN_WIDTH) - 1)
    dut.input_last.value = 1
    dut.input_valid.value = 1
    dut.output_ready.value = 1
    got = None
    for _ in range(64):
        await ReadOnly()
        if int(dut.output_valid.value) == 1:
            got = (int(dut.output_data.value), int(dut.output_last.value))
        fired = int(dut.input_ready.value) == 1
        await RisingEdge(dut.clk)
        if got is not None:
            break
        if fired:
            dut.input_valid.value = 0
    dut.input_valid.value = 0
    dut.output_ready.value = 0
    assert got is not None, "a complete short final beat was never forwarded"
    data, last = got
    assert last == 1, "the short final beat must carry output_last"
    # the presented rows come back byte-identically, the unfilled half is PAD
    assert data & ((1 << IN_WIDTH) - 1) == beat & ((1 << IN_WIDTH) - 1), "bypass altered the forwarded rows"
    assert data >> IN_WIDTH == (1 << IN_WIDTH) - 1, "the unfilled upper half must be PAD"
    # and it must NOT have raised a stream error
    dut.status_ready.value = 1
    for _ in range(16):
        await ReadOnly()
        assert not (int(dut.status_valid.value) == 1 and int(dut.status_error.value) == 1), "a complete final beat was reported torn"
        await RisingEdge(dut.clk)
    dut.status_ready.value = 0


@cocotb.test()
async def a_layout_that_could_forge_the_pad_is_rejected(dut):
    """REGRESSION. The all-ones packed word is the ragged-tail PAD at
    SLOTS > 1. A field layout covering all 64 bits can PRODUCE all-ones from
    real data -- four unsigned 16-bit fields at max -- and that row would be
    dropped by the wide tiles. The same input unpacks normally at SLOTS == 1,
    so the ANSWER WOULD DEPEND ON OUT_WIDTH.

    Rejecting the layout makes the sentinel unreachable by construction
    rather than by convention. Every real packing has room: SF1 q6 is 56
    bits, q1 is 41.
    """
    if SLOTS == 1:
        return  # no pad convention at one row per beat, so no collision
    await _start(dut)
    full = [UnpackFieldSpec(offset=off, width=16) for off in (0, 16, 32, 48)]  # 64 bits, no headroom
    _drive_config(dut, full)
    send = cocotb.start_soon(_send_beats(dut, [(1 << IN_WIDTH) - 1]))
    dut.status_ready.value = 1
    code = None
    for _ in range(256):
        await ReadOnly()
        if int(dut.status_valid.value) == 1:
            code = (int(dut.status_error.value), int(dut.status_error_code.value))
        await RisingEdge(dut.clk)
        if code is not None:
            break
    dut.status_ready.value = 0
    await send
    assert code == (1, int(UnpackErrorCode.CONFIG)), f"a pad-forging layout must be refused as a bad config, got {code}"

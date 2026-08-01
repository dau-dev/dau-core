"""Record gearbox: wide beats in, one WHOLE RECORD PER CYCLE out.

Correctness here is nearly trivial — the gearbox is a pure reframing, so
the golden is the identity on the record sequence. The property that
justifies the core is the RATE, so this bench measures it: under
continuous valid input and an always-ready sink it must sustain one
record per cycle, which is what turns a 3-cycles-per-record front into a
1-cycle one. A version that was correct but slow would pass a
values-only bench and be worthless."""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import gearbox_records

_MASK64 = (1 << 64) - 1


def _beat_words(dut) -> int:
    return len(dut.input_data) // 64


def _record_words(dut) -> int:
    return len(dut.output_data) // 64


async def _start(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, 4, units="ns").start())
    dut.rst.value = 1
    dut.cfg_input_records.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


def _beats(words, beat_words):
    padded = list(words)
    while len(padded) % beat_words:
        padded.append(0xDEAD_BEEF_DEAD_BEEF)  # tail padding, never a record
    return [padded[i : i + beat_words] for i in range(0, len(padded), beat_words)]


async def _send(dut, beats, *, gap_pattern=(1,)) -> None:
    for index, beat in enumerate(beats):
        value = 0
        for w, word in enumerate(beat):
            value |= (word & _MASK64) << (64 * w)
        if not gap_pattern[index % len(gap_pattern)]:
            # a bounded idle gap. Looping on a FIXED index would never
            # terminate — the original spin that looked like an RTL hang.
            dut.input_valid.value = 0
            await RisingEdge(dut.clk)
        dut.input_data.value = value
        dut.input_last.value = int(index == len(beats) - 1)
        dut.input_valid.value = 1
        for spin in range(5000):
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            state = (
                int(dut.filled.value),
                int(dut.records_left.value),
                int(dut.saw_last.value),
                int(dut.status_valid.value),
                int(dut.output_valid.value),
            )
            await RisingEdge(dut.clk)
            if fired:
                break
        else:
            raise AssertionError(f"beat {index} never accepted: filled/left/saw_last/status/out_valid = {state}")
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _collect(dut, count, *, ready_pattern=(1,)):
    got, first, last_tick = [], None, None
    saw_last_flag = [False]
    for tick in range(4_000):
        dut.output_ready.value = ready_pattern[tick % len(ready_pattern)]
        await ReadOnly()
        fired = int(dut.output_valid.value) == 1 and int(dut.output_ready.value) == 1
        data = int(dut.output_data.value)
        is_last = int(dut.output_last.value)
        await RisingEdge(dut.clk)
        if fired:
            words = tuple((data >> (64 * w)) & _MASK64 for w in range(_record_words(dut)))
            got.append(words)
            first = tick if first is None else first
            last_tick = tick
            if is_last:
                saw_last_flag[0] = True
                break
        if len(got) == count:
            break
    dut.output_ready.value = 0
    # output_last must actually have ridden the final record: breaking on the
    # count alone would pass a gearbox with output_last hardwired low
    if len(got) == count and not saw_last_flag[0]:
        raise AssertionError(f"collected {count} records but output_last never asserted")
    if len(got) != count:
        raise AssertionError(
            f"collected {len(got)}/{count} records; DUT filled={int(dut.filled.value)} "
            f"left={int(dut.records_left.value)} saw_last={int(dut.saw_last.value)} status={int(dut.status_valid.value)}"
        )
    cycles = (last_tick - first + 1) if (first is not None and last_tick is not None) else 0
    return got, cycles


def _records(rng, count, record_words):
    return [tuple(rng.randrange(1, 1 << 60) for _ in range(record_words)) for _ in range(count)]


async def _run(dut, records, *, ready_pattern=(1,), gap_pattern=(1,)):
    flat = [w for record in records for w in record]
    # the record count must be VISIBLE to the DUT before the first beat is
    # accepted: the gearbox latches it on that beat, and a write made in the
    # same delta would latch a stale zero — the gearbox would then owe no
    # records, never emit, fill its window and stall the sender forever.
    dut.cfg_input_records.value = len(records)
    await RisingEdge(dut.clk)
    send = cocotb.start_soon(_send(dut, _beats(flat, _beat_words(dut)), gap_pattern=gap_pattern))
    got, cycles = await _collect(dut, len(records), ready_pattern=ready_pattern)
    await send
    expected = gearbox_records(flat, record_words=_record_words(dut), record_count=len(records))
    assert got == expected, f"reframing mismatch: {len(got)} records vs {len(expected)}"
    return got, cycles


@cocotb.test()
async def records_reframe_across_beat_boundaries(dut):
    """The interesting case is a record size that does NOT divide the beat:
    3-word records over 8-word beats straddle every boundary."""
    await _start(dut)
    rng = random.Random(20260801)
    await _run(dut, _records(rng, 64, _record_words(dut)))


@cocotb.test()
async def sustains_one_record_per_cycle(dut):
    """THE property this core exists for. A 64-bit front costs N cycles per
    N-word record; this must cost one, or the whole point is lost."""
    await _start(dut)
    rng = random.Random(7)
    records = _records(rng, 256, _record_words(dut))
    _, cycles = await _run(dut, records)
    # the gearbox emits at most one record per cycle, but it can never beat
    # what the input bus delivers: BEAT_WORDS words per cycle against
    # RECORD_WORDS per record. At 512 bits / 3-word records that is 2.67
    # records available per cycle so 1/cycle is achievable; at 128 bits the
    # bus itself allows only 2/3 of a record per cycle. Assert the achievable
    # rate, which is what the composition's throughput model also predicts.
    beat_words, record_words = _beat_words(dut), _record_words(dut)
    per_cycle = min(1.0, beat_words / record_words)
    budget = len(records) / per_cycle + 8
    assert cycles <= budget, f"emitted {len(records)} records in {cycles} cycles, budget {budget:.0f} at {per_cycle:.2f} rec/cycle"


@cocotb.test()
async def a_ragged_tail_beat_emits_no_padding_record(dut):
    """The final beat is padded to the bus width; the record count — not the
    byte length — decides how many records exist."""
    await _start(dut)
    rng = random.Random(11)
    beat_words = _beat_words(dut)
    record_words = _record_words(dut)
    # a count whose words leave the last beat partly padded
    count = beat_words + 1 if (beat_words + 1) * record_words % beat_words else beat_words + 2
    got, _ = await _run(dut, _records(rng, count, record_words))
    assert len(got) == count


@cocotb.test()
async def backpressure_and_input_gaps_stay_golden(dut):
    await _start(dut)
    rng = random.Random(20260802)
    await _run(dut, _records(rng, 48, _record_words(dut)), ready_pattern=(1, 0, 0), gap_pattern=(1, 1, 0))


@cocotb.test()
async def back_to_back_batches_rearm(dut):
    await _start(dut)
    rng = random.Random(3)
    for _ in range(3):
        await _run(dut, _records(rng, 20, _record_words(dut)))


@cocotb.test()
async def a_short_stream_reports_a_framing_error(dut):
    """The record COUNT and the byte length must agree. Nothing else in this
    bench exercises the error branch — without this, deleting it entirely
    would still pass."""
    await _start(dut)
    rng = random.Random(31)
    record_words = _record_words(dut)
    beat_words = _beat_words(dut)
    records = _records(rng, 4, record_words)
    flat = [w for record in records for w in record]
    # send the whole stream but CLAIM far more records than its words can
    # supply. Overclaiming by a full beat's worth guarantees the shortfall
    # cannot hide inside tail padding, and keeps input_last on a real beat
    # (stripping beats instead can leave nothing to frame the batch).
    beats = _beats(flat, beat_words)
    dut.cfg_input_records.value = len(records) + 2 * beat_words
    await RisingEdge(dut.clk)
    dut.status_ready.value = 1
    send = cocotb.start_soon(_send(dut, beats))
    dut.output_ready.value = 1
    for _ in range(4_000):
        await ReadOnly()
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == 2  # ERR_STREAM
            await RisingEdge(dut.clk)
            break
        await RisingEdge(dut.clk)
    else:
        raise AssertionError("a stream shorter than its record count never reported ERR_STREAM")
    await send

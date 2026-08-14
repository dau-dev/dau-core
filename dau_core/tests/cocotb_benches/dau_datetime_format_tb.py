"""The datetime formatter against its golden, character for character.

This tile shipped without a bench, which an audit caught: its only test read
constants out of the SystemVerilog and exercised the Python golden, so the
HDL was never instantiated. A resource number for a module nobody ran is a
number about an unverified design, and that is what this file fixes.

Every case compares the 152-bit output beat against ``format_datetime_rows``
by decoding it back to a string, so a wrong character fails with the two
strings side by side rather than with a hex mismatch.

The dates chosen are the ones the era arithmetic gets wrong when it is
written by hand: 2000 is a leap year (divisible by 400), 2100 is not
(divisible by 100 but not 400), and 2024-02-29 must exist. The range ends
are here because a formatter that silently truncates the year is exactly the
failure ``DOMAIN`` exists to prevent.
"""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import (
    DATETIME_FORMAT_MAX_EPOCH_SECONDS,
    DatetimeFormatErrorCode,
    format_datetime_rows,
)
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

_CHARS = 19

# Instants the 400-year era arithmetic gets wrong when hand-written.
_EDGE_ROWS = [
    0,  # 1970-01-01T00:00:00, the low bound
    951_782_400,  # 2000-02-29 -- a leap year because 400 divides it
    4_107_542_400,  # 2100-02-28 -- NOT a leap year: 100 divides it, 400 does not
    1_709_164_800,  # 2024-02-29 -- an ordinary leap year
    DATETIME_FORMAT_MAX_EPOCH_SECONDS,  # 9999-12-31T23:59:59, the high bound
]


def _decode(beat: int) -> str:
    """The 152-bit beat back to text: character i in bits [8*i+7:8*i]."""
    return "".join(chr((beat >> (8 * index)) & 0xFF) for index in range(_CHARS))


async def _start(dut):
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    for _ in range(5):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _send(dut, rows) -> None:
    """Drive rows, one handshake at a time.

    Sampling happens in ReadOnly and the cycle advances on RisingEdge, which
    is the palette's shape. Combining send, drain and status-watch into one
    loop deadlocks -- the sender holds input_valid while the same loop is
    trying to observe the output, so neither side ever fires.
    """
    for index, row in enumerate(rows):
        dut.input_data.value = row
        dut.input_last.value = int(index == len(rows) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _run(dut, rows, *, expect: int | None = None, ready_pattern=None):
    """Send ``rows`` concurrently while draining.

    **This tile declares ``success_close_out: False``**, so a clean batch is
    contractually SILENT -- there is no status beat to wait for, and a bench
    that waits for one hangs forever. That is not a tile defect; 23 of the 45
    registered operators are silent on success, and the registry fact is the
    authority. So the drain ends when the expected rows have arrived, and
    only an ERROR produces a close-out.

    ``expect`` is how many rendered rows to wait for (default: all of them).
    ``ready_pattern`` stalls the sink, which is the backpressure case.
    """
    want = len(rows) if expect is None else expect
    seen: list[str] = []
    cocotb.start_soon(_send(dut, rows))
    status: tuple[bool, int] | None = None
    cycle = 0
    settle = 0
    while True:
        cycle += 1
        if cycle > 20_000:
            raise AssertionError(f"timed out with {len(seen)} of {want} rows and status={status}")
        dut.output_ready.value = 1 if ready_pattern is None else int(bool(ready_pattern(cycle)))
        dut.status_ready.value = 1
        await ReadOnly()
        if int(dut.output_valid.value) == 1 and int(dut.output_ready.value) == 1:
            seen.append(_decode(int(dut.output_data.value)))
        if int(dut.status_valid.value) == 1:
            status = (bool(int(dut.status_error.value)), int(dut.status_error_code.value))
        await RisingEdge(dut.clk)
        if status is not None:
            break
        if len(seen) >= want:
            # let a late close-out land rather than racing past it
            settle += 1
            if settle > 8:
                break
    dut.output_ready.value = 1
    return seen, status


@cocotb.test()
async def renders_every_row_exactly(dut):
    await _start(dut)
    rows = [0, 1_700_000_000, 946_684_800, 1_234_567_890]
    seen, status = await _run(dut, rows)
    assert status is None, f"a silent-on-success tile must not close out a clean batch; got {status}"
    assert seen == format_datetime_rows(rows), f"{seen} != {format_datetime_rows(rows)}"


@cocotb.test()
async def leap_rules_and_range_ends_are_exact(dut):
    """The cases a hand-written civil-from-days gets wrong."""
    await _start(dut)
    seen, status = await _run(dut, _EDGE_ROWS)
    assert status is None, f"unexpected close-out: {status}"
    expected = format_datetime_rows(_EDGE_ROWS)
    for got, want, epoch in zip(seen, expected, _EDGE_ROWS, strict=True):
        assert got == want, f"epoch {epoch}: {got!r} != {want!r}"


@cocotb.test()
async def a_value_past_the_range_closes_out_domain(dut):
    """A formatter that truncated the year instead of refusing would pass
    every other test in this file, which is why this one exists."""
    await _start(dut)
    seen, status = await _run(dut, [0, DATETIME_FORMAT_MAX_EPOCH_SECONDS + 1, 0], expect=0)
    assert status is not None
    error, code = status
    assert error, "an out-of-range instant must close the batch out as an error"
    assert code == int(DatetimeFormatErrorCode.DOMAIN), f"expected DOMAIN, got {code}"
    # An error invalidates the WHOLE batch: nothing is emitted, not even the
    # rows that preceded the bad one. That is stronger than refusing to
    # render the offending row, and it is the right contract -- a caller
    # reading rows that arrived before the close-out would otherwise take a
    # partial answer for a complete one.
    assert seen == [], f"an invalidated batch must emit nothing, got {seen}"


@cocotb.test()
async def backpressure_does_not_drop_or_reorder(dut):
    """The sink stalls on a fixed pattern; nothing may be lost or reordered."""
    await _start(dut)
    rows = [index * 86_400 for index in range(1, 33)]
    seen, status = await _run(dut, rows, ready_pattern=lambda cycle: cycle % 3 != 0)
    assert status is None, f"unexpected close-out: {status}"
    assert seen == format_datetime_rows(rows), "backpressure changed the result"


@cocotb.test()
async def a_second_batch_matches_the_first(dut):
    """Repeated runs without an intervening reset: state must not carry."""
    await _start(dut)
    rows = [0, 951_782_400, 1_709_164_800]
    first, first_status = await _run(dut, rows)
    second, second_status = await _run(dut, rows)
    assert first_status is None and second_status is None
    assert first == second == format_datetime_rows(rows)


@cocotb.test()
async def reset_clears_a_batch_in_flight(dut):
    """Reset mid-batch, then run a clean one: the interrupted rows must not
    appear in the next batch's output."""
    await _start(dut)
    dut.input_valid.value = 1
    dut.input_data.value = 1_700_000_000
    dut.input_last.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    await _start(dut)  # asserts rst again
    rows = [0, 86_400]
    seen, status = await _run(dut, rows)
    assert status is None, f"unexpected close-out: {status}"
    assert seen == format_datetime_rows(rows), "state survived reset"


@cocotb.test()
async def randomized_instants_match_the_golden(dut):
    await _start(dut)
    rng = random.Random(7)
    rows = [rng.randint(0, DATETIME_FORMAT_MAX_EPOCH_SECONDS) for _ in range(128)]
    seen, status = await _run(dut, rows)
    assert status is None, f"unexpected close-out: {status}"
    for got, want, epoch in zip(seen, format_datetime_rows(rows), rows, strict=True):
        assert got == want, f"epoch {epoch}: {got!r} != {want!r}"


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    rows = [index * 3_600 for index in range(256)]

    async def send():
        for index, row in enumerate(rows):
            dut.input_valid.value = 1
            dut.input_data.value = row
            dut.input_last.value = 1 if index == len(rows) - 1 else 0
            await RisingEdge(dut.clk)
            while True:
                await ReadOnly()
                ready = dut.input_ready.value == 1
                await RisingEdge(dut.clk)
                if ready:
                    break
        dut.input_valid.value = 0
        dut.input_last.value = 0

    await assert_sustained_ingest(dut, "datetime-format", send=send, input_beats=len(rows))

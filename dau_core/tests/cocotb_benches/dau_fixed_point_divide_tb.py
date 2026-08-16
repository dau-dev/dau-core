"""The fixed-point divide tile against its golden, with NO tolerance.

The quotient is exact by construction -- `(A << F) // B` -- and FLOOR, which
is the convention MULSHR already set with its arithmetic shift. The negative
cases here are the ones that would catch a tile that truncated toward zero
instead, because that is the plausible wrong choice.
"""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import DivideErrorCode, RowDivideConfig, RowField, divide_row_fields
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

_MASK32 = 0xFFFF_FFFF
_INT32_MAX = (1 << 31) - 1
_INT32_MIN = -(1 << 31)
_ONE = 1 << 16  # 1.0 in Q16.16

# (key, value, aux0, aux1); value / aux0
_ROWS = [
    (1, 6 * _ONE, 2 * _ONE, 0),
    (2, 1 * _ONE, 3 * _ONE, 0),
    (3, 0, 5 * _ONE, 0),
    (4, -7 * _ONE, 2 * _ONE, 0),
]

_DIVIDE = RowDivideConfig(dst=RowField.AUX1, numerator=RowField.VALUE, denominator=RowField.AUX0)


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    rows = [(index, index * 7919, (index + 1) * _ONE, 0) for index in range(256)]
    _drive_config(dut, _DIVIDE)
    words = _pack_rows(rows)
    await assert_sustained_ingest(
        dut,
        "fixed-point-divide",
        send=lambda: _send_words(dut, words),
        input_beats=len(words),
        beats_per_row=2,
    )


@cocotb.test()
async def exact_quotients_are_exact(dut):
    """Cases a human can check by hand, so a wrong scale factor cannot hide."""
    await _start(dut)
    rows = [(0, a * _ONE, b * _ONE, 0) for a, b in ((6, 2), (100, 4), (7, 7), (1, 1))]
    out = await _run(dut, rows, _DIVIDE)
    assert out == divide_row_fields(rows, _DIVIDE)
    for (a, b), row in zip(((6, 2), (100, 4), (7, 7), (1, 1)), out):
        assert row[RowField.AUX1] == (a // b) * _ONE, f"{a}/{b} should be exactly {a // b}.0"


@cocotb.test()
async def negative_operands_floor_rather_than_truncate(dut):
    """The convention check. Truncation toward zero is the plausible wrong
    answer and differs from the golden on every inexact negative quotient."""
    await _start(dut)
    rows = [
        (1, -7 * _ONE, 2 * _ONE, 0),
        (2, 7 * _ONE, -2 * _ONE, 0),
        (3, -7 * _ONE, -2 * _ONE, 0),
        (4, -1, 3 * _ONE, 0),  # a tiny negative over a positive: floors to -1
    ]
    out = await _run(dut, rows, _DIVIDE)
    assert out == divide_row_fields(rows, _DIVIDE)
    assert out[3][RowField.AUX1] == -1, "an inexact negative quotient must floor, not truncate to 0"


@cocotb.test()
async def directed_rows_match_golden(dut):
    await _start(dut)
    assert await _run(dut, _ROWS, _DIVIDE) == divide_row_fields(_ROWS, _DIVIDE)


@cocotb.test()
async def dst_may_be_either_source_and_updates_in_place(dut):
    await _start(dut)
    for dst in (RowField.VALUE, RowField.AUX0):
        config = RowDivideConfig(dst=dst, numerator=RowField.VALUE, denominator=RowField.AUX0)
        assert await _run(dut, _ROWS, config) == divide_row_fields(_ROWS, config), dst.name


@cocotb.test()
async def boundaries_around_every_power_of_two(dut):
    await _start(dut)
    rows = []
    index = 0
    for shift in range(0, 31, 3):
        for delta in (-1, 0, 1):
            numerator = (1 << shift) + delta
            for denominator in (_ONE, 3 * _ONE, (1 << 20)):
                if abs(numerator) < abs(denominator) << 15:
                    rows.append((index, numerator, denominator, 0))
                    index += 1
    assert await _run(dut, rows, _DIVIDE) == divide_row_fields(rows, _DIVIDE)


@cocotb.test()
async def order_and_count_preserved_with_backpressure(dut):
    await _start(dut)
    out = await _run(dut, _ROWS, _DIVIDE, ready_pattern=(1, 0, 0, 1, 0))
    assert out == divide_row_fields(_ROWS, _DIVIDE)


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0xD117)
    for trial in range(30):
        rows = []
        while len(rows) < rng.randint(1, 8):
            numerator = rng.choice((rng.randint(-(1 << 20), 1 << 20), rng.randint(_INT32_MIN, _INT32_MAX)))
            denominator = rng.choice((rng.randint(1, 1 << 20), rng.randint(_INT32_MIN, _INT32_MAX)))
            if denominator == 0:
                continue
            quotient = (numerator << 16) // denominator
            if not _INT32_MIN <= quotient <= _INT32_MAX:
                continue  # the RANGE close-out is covered on its own
            rows.append((rng.randint(-1000, 1000), numerator, denominator, 0))
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):
            pattern = pattern + (1,)
        out = await _run(dut, rows, _DIVIDE, ready_pattern=pattern)
        assert out == divide_row_fields(rows, _DIVIDE), f"trial {trial}: rows={rows}"


@cocotb.test()
async def zero_divisor_reports_domain_error(dut):
    await _start(dut)
    _drive_config(dut, _DIVIDE)
    rows = [(1, 6 * _ONE, 2 * _ONE, 0), (2, 1 * _ONE, 0, 0), (3, 4 * _ONE, _ONE, 0)]
    seen: list[int] = []
    drain = cocotb.start_soon(_drain_and_record(dut, seen))
    send = cocotb.start_soon(_send_rows(dut, rows))
    await _expect_status(dut, error=True, code=DivideErrorCode.DOMAIN)
    await send
    drain.kill()
    dut.output_ready.value = 0

    assert 2 not in [_signed32(word & _MASK32) for word in seen[0::2]], "the zero-divisor row was emitted"
    assert await _run(dut, _ROWS, _DIVIDE) == divide_row_fields(_ROWS, _DIVIDE)


@cocotb.test()
async def overflowing_quotient_reports_range_error(dut):
    """1.0 / (1 * 2**-16) is 2**32: defined, but not an int32. The tile must
    refuse rather than wrap or saturate."""
    await _start(dut)
    _drive_config(dut, _DIVIDE)
    rows = [(1, 6 * _ONE, 2 * _ONE, 0), (2, _ONE, 1, 0), (3, 4 * _ONE, _ONE, 0)]
    seen: list[int] = []
    drain = cocotb.start_soon(_drain_and_record(dut, seen))
    send = cocotb.start_soon(_send_rows(dut, rows))
    await _expect_status(dut, error=True, code=DivideErrorCode.RANGE)
    await send
    drain.kill()
    dut.output_ready.value = 0

    assert 2 not in [_signed32(word & _MASK32) for word in seen[0::2]], "the overflowing row was emitted"
    assert await _run(dut, _ROWS, _DIVIDE) == divide_row_fields(_ROWS, _DIVIDE)


@cocotb.test()
async def the_representable_negative_extreme_is_accepted(dut):
    """The range check is inclusive only when the signs agree. -2**31 is
    representable and must go through; +2**31 is one too large and must not.
    A single symmetric comparison would get one of these wrong."""
    await _start(dut)
    rows = [(1, 1 << 15, -1, 0)]
    out = await _run(dut, rows, _DIVIDE)
    assert out == divide_row_fields(rows, _DIVIDE)
    assert out[0][RowField.AUX1] == _INT32_MIN

    _drive_config(dut, _DIVIDE)
    send = cocotb.start_soon(_send_rows(dut, [(2, 1 << 15, 1, 0)]))
    await _expect_status(dut, error=True, code=DivideErrorCode.RANGE)
    await send


@cocotb.test()
async def refused_rows_never_escape_while_the_close_out_waits(dut):
    """The ingest guards are only load-bearing under status backpressure.

    Taken promptly, the close-out flushes the pipeline and a refused row
    would be dropped before it retired, making the guard look redundant.
    Hold status_ready low for longer than the pipeline is deep and the row
    reaches the output instead -- which is the case that must not emit a
    quotient the tile does not have.
    """
    for label, bad_row, code in (
        ("zero divisor", (2, _ONE, 0, 0), DivideErrorCode.DOMAIN),
        ("overflow", (2, _ONE, 1, 0), DivideErrorCode.RANGE),
    ):
        await _start(dut)
        _drive_config(dut, _DIVIDE)
        dut.status_ready.value = 0
        rows = [(1, 6 * _ONE, 2 * _ONE, 0), bad_row, (3, 4 * _ONE, _ONE, 0)]
        seen: list[int] = []
        drain = cocotb.start_soon(_drain_and_record(dut, seen))
        send = cocotb.start_soon(_send_rows(dut, rows))

        # ITERATIONS stages that advance once every three cycles, plus the
        # emit handshake -- a tight bound here made the guard look untested
        for _ in range(400):
            await RisingEdge(dut.clk)

        keys = [_signed32(word & _MASK32) for word in seen[0::2]]
        assert 2 not in keys, f"{label}: the refused row escaped while the close-out waited: keys {keys}"

        await _expect_status(dut, error=True, code=code)
        await send
        drain.kill()
        dut.output_ready.value = 0


@cocotb.test()
async def torn_row_reports_stream_error(dut):
    await _start(dut)
    _drive_config(dut, _DIVIDE)
    await _send_words(dut, [(_ONE << 32) | 5])
    await _expect_status(dut, error=True, code=DivideErrorCode.STREAM)

    assert await _run(dut, _ROWS, _DIVIDE) == divide_row_fields(_ROWS, _DIVIDE)


@cocotb.test()
async def error_status_survives_backpressure(dut):
    await _start(dut)
    _drive_config(dut, _DIVIDE)
    dut.status_ready.value = 0
    await _send_words(dut, [(_ONE << 32) | 5])

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
        assert int(dut.status_error_code.value) == DivideErrorCode.STREAM
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=True, code=DivideErrorCode.STREAM)
    assert await _run(dut, _ROWS, _DIVIDE) == divide_row_fields(_ROWS, _DIVIDE)


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _drive_config(dut, config: RowDivideConfig) -> None:
    dut.cfg_dst_field.value = int(config.dst)
    dut.cfg_numerator_field.value = int(config.numerator)
    dut.cfg_denominator_field.value = int(config.denominator)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    _drive_config(dut, _DIVIDE)
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, config: RowDivideConfig, *, ready_pattern=(1,)):
    _drive_config(dut, config)
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_output(dut, ready_pattern, expect_words=2 * len(rows))
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


async def _drain_and_record(dut, seen: list[int]) -> None:
    """Drain the output, keeping what came out so a test can assert on it."""
    dut.output_ready.value = 1
    while True:
        await ReadOnly()
        if int(dut.output_valid.value) == 1:
            seen.append(int(dut.output_data.value))
        await RisingEdge(dut.clk)


async def _drain_words(dut) -> None:
    dut.output_ready.value = 1
    while True:
        await RisingEdge(dut.clk)


async def _collect_output(dut, ready_pattern, *, expect_words: int) -> list[int]:
    words: list[int] = []
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
            assert valid, "output_valid dropped while a beat was stalled"
            assert (word, last) == held, "output beat changed while stalled"
        held = None if (fired or not valid) else (word, last)
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                dut.output_ready.value = 0
                assert len(words) == expect_words, f"expected {expect_words} words, got {len(words)}"
                await _assert_quiescent(dut)
                return words
    raise AssertionError(f"timed out collecting divide output ({len(words)}/{expect_words} words)")


async def _assert_quiescent(dut) -> None:
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
            dut.status_ready.value = 0
            assert observed_error == int(error), f"status error {observed_error} != {int(error)}"
            assert observed_code == int(code), f"status code {observed_code} != {int(code)}"
            return
    raise AssertionError("timed out waiting for a status beat")

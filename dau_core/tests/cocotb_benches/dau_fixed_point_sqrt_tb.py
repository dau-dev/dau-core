"""The fixed-point sqrt tile against its golden, with NO tolerance.

The root is exact by construction -- `isqrt(X << F)` -- so every comparison
here is equality against `sqrt_row_fields`. If this tile ever grows a
polynomial approximation, these tests are what will refuse it, and that is
deliberate: an approximate root would need an error bound nobody in the
stack currently has a way to state.
"""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import RowField, RowSqrtConfig, SqrtErrorCode, sqrt_row_fields
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

_MASK32 = 0xFFFF_FFFF
_INT32_MAX = (1 << 31) - 1
_ONE = 1 << 16  # 1.0 in Q16.16

# (key, value, aux0, aux1); value carries the radicand
_ROWS = [
    (568, 4 * _ONE, 3, 0),  # a perfect square: 2.0
    (569, 2 * _ONE, 4, 0),  # irrational: floor-truncated
    (568, 0, 5, 0),  # zero is in the domain
    (570, _INT32_MAX, 6, 0),  # the largest radicand
]

_SQRT_VALUE = RowSqrtConfig(dst=RowField.AUX1, src=RowField.VALUE)


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    rows = [(index, index * 7919, index + 2, 0) for index in range(256)]
    _drive_config(dut, _SQRT_VALUE)
    words = _pack_rows(rows)
    await assert_sustained_ingest(
        dut,
        "fixed-point-sqrt",
        send=lambda: _send_words(dut, words),
        input_beats=len(words),
        beats_per_row=2,
    )


@cocotb.test()
async def perfect_squares_are_exact(dut):
    """The cases a human can check by hand, so a wrong scale factor cannot
    hide behind 'close enough'."""
    await _start(dut)
    rows = [(0, (n * n) * _ONE, 0, 0) for n in (1, 2, 3, 16, 100, 181)]
    out = await _run(dut, rows, _SQRT_VALUE)
    assert out == sqrt_row_fields(rows, _SQRT_VALUE)
    for n, row in zip((1, 2, 3, 16, 100, 181), out):
        assert row[RowField.AUX1] == n * _ONE, f"sqrt({n}^2) should be exactly {n}.0"


@cocotb.test()
async def directed_rows_match_golden(dut):
    await _start(dut)
    assert await _run(dut, _ROWS, _SQRT_VALUE) == sqrt_row_fields(_ROWS, _SQRT_VALUE)


@cocotb.test()
async def every_dst_variant_passes_the_others_through(dut):
    await _start(dut)
    for dst in RowField:
        if dst == RowField.VALUE:
            continue  # in place, so "passes the others through" does not apply
        config = RowSqrtConfig(dst=dst, src=RowField.VALUE)
        out = await _run(dut, _ROWS, config)
        assert out == sqrt_row_fields(_ROWS, config), dst.name
        for row, rooted in zip(_ROWS, out):
            for field in RowField:
                if field != dst:
                    assert rooted[field] == row[field], f"{dst.name} clobbered {field.name}"


@cocotb.test()
async def boundaries_around_every_power_of_two(dut):
    """Digit recurrences fail at carries. Each power of two and its
    neighbours put the recurrence's remainder at its extremes."""
    await _start(dut)
    values = []
    for shift in range(31):
        for delta in (-1, 0, 1):
            candidate = (1 << shift) + delta
            if 0 <= candidate <= _INT32_MAX:
                values.append(candidate)
    rows = [(index, value, 0, 0) for index, value in enumerate(values)]
    assert await _run(dut, rows, _SQRT_VALUE) == sqrt_row_fields(rows, _SQRT_VALUE)


@cocotb.test()
async def order_and_count_preserved_with_backpressure(dut):
    await _start(dut)
    out = await _run(dut, _ROWS, _SQRT_VALUE, ready_pattern=(1, 0, 0, 1, 0))
    assert out == sqrt_row_fields(_ROWS, _SQRT_VALUE)


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0x5017)
    for trial in range(30):
        rows = []
        for _ in range(rng.randint(1, 10)):
            if rng.random() < 0.15:
                value = rng.choice((0, 1, _ONE, _INT32_MAX, _INT32_MAX - 1, 1 << 30))
            else:
                value = rng.randint(0, _INT32_MAX)
            rows.append((rng.randint(-1000, 1000), value, rng.randint(-1000, 1000), 0))
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):
            pattern = pattern + (1,)
        out = await _run(dut, rows, _SQRT_VALUE, ready_pattern=pattern)
        assert out == sqrt_row_fields(rows, _SQRT_VALUE), f"trial {trial}: rows={rows}"


@cocotb.test()
async def negative_source_drains_batch_then_reports_domain_error(dut):
    """The whole reason the tile has its own error code: sqrt of a negative
    has no answer, and emitting one anyway would be a wrong number that
    looks right."""
    await _start(dut)
    _drive_config(dut, _SQRT_VALUE)
    rows = [(1, 4 * _ONE, 0, 0), (2, -1, 0, 0), (3, 9 * _ONE, 0, 0)]
    seen: list[int] = []
    drain = cocotb.start_soon(_drain_and_record(dut, seen))
    send = cocotb.start_soon(_send_rows(dut, rows))
    await _expect_status(dut, error=True, code=SqrtErrorCode.DOMAIN)
    await send
    drain.kill()
    dut.output_ready.value = 0

    # The offending row must never appear. Rows ahead of it in the batch may
    # already be out -- the error close-out invalidates the batch downstream --
    # but the tile must not emit a root it does not have. Its key identifies it.
    emitted_keys = [_signed32(word & _MASK32) for word in seen[0::2]]
    assert 2 not in emitted_keys, f"the negative row was emitted with a substituted root: keys {emitted_keys}"

    # recovers: the next batch roots normally
    assert await _run(dut, _ROWS, _SQRT_VALUE) == sqrt_row_fields(_ROWS, _SQRT_VALUE)


@cocotb.test()
async def negative_row_never_escapes_while_the_close_out_waits(dut):
    """The domain guard is only load-bearing under status backpressure.

    When the close-out is taken promptly it flushes the pipeline, so a
    negative row that entered would be dropped before it retired and the
    guard would look redundant. Hold status_ready low for longer than the
    pipeline is deep and the row reaches the output instead -- which is
    exactly the case that must not emit a root the tile does not have.
    """
    await _start(dut)
    _drive_config(dut, _SQRT_VALUE)
    dut.status_ready.value = 0
    rows = [(1, 4 * _ONE, 0, 0), (2, -1, 0, 0), (3, 9 * _ONE, 0, 0)]
    seen: list[int] = []
    drain = cocotb.start_soon(_drain_and_record(dut, seen))
    send = cocotb.start_soon(_send_rows(dut, rows))

    # longer than ITERATIONS, so anything that entered the pipeline has had
    # time to come out the far end
    for _ in range(120):
        await RisingEdge(dut.clk)

    emitted_keys = [_signed32(word & _MASK32) for word in seen[0::2]]
    assert 2 not in emitted_keys, f"the negative row escaped while the close-out waited: keys {emitted_keys}"

    await _expect_status(dut, error=True, code=SqrtErrorCode.DOMAIN)
    await send
    drain.kill()
    dut.output_ready.value = 0

    assert await _run(dut, _ROWS, _SQRT_VALUE) == sqrt_row_fields(_ROWS, _SQRT_VALUE)


@cocotb.test()
async def dst_equal_to_src_updates_in_place(dut):
    """In-place is legal, as it is for the map ALU: the source is captured
    when the row is pushed and the destination written when it retires, so
    they may be the same field."""
    await _start(dut)
    config = RowSqrtConfig(dst=RowField.VALUE, src=RowField.VALUE)
    rows = [(1, 4 * _ONE, 0, 0), (2, 9 * _ONE, 0, 0), (3, 2 * _ONE, 0, 0)]
    out = await _run(dut, rows, config)
    assert out == sqrt_row_fields(rows, config)
    assert out[0][RowField.VALUE] == 2 * _ONE
    assert out[1][RowField.VALUE] == 3 * _ONE


@cocotb.test()
async def torn_row_reports_stream_error(dut):
    await _start(dut)
    _drive_config(dut, _SQRT_VALUE)
    await _send_words(dut, [(_ONE << 32) | 5])  # input_last on a w0
    await _expect_status(dut, error=True, code=SqrtErrorCode.STREAM)

    assert await _run(dut, _ROWS, _SQRT_VALUE) == sqrt_row_fields(_ROWS, _SQRT_VALUE)


@cocotb.test()
async def error_status_survives_backpressure(dut):
    await _start(dut)
    _drive_config(dut, _SQRT_VALUE)
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
        assert int(dut.status_error.value) == 1
        assert int(dut.status_error_code.value) == SqrtErrorCode.STREAM
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=True, code=SqrtErrorCode.STREAM)
    assert await _run(dut, _ROWS, _SQRT_VALUE) == sqrt_row_fields(_ROWS, _SQRT_VALUE)


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _drive_config(dut, config: RowSqrtConfig) -> None:
    dut.cfg_dst_field.value = int(config.dst)
    dut.cfg_src_field.value = int(config.src)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    _drive_config(dut, _SQRT_VALUE)
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, config: RowSqrtConfig, *, ready_pattern=(1,)):
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
    raise AssertionError(f"timed out collecting sqrt output ({len(words)}/{expect_words} words)")


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

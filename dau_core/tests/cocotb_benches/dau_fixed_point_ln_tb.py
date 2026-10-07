"""The rendered ln tile against the model it was rendered from.

Zero tolerance: the HDL mirrors LnDatapath.evaluate step for step, so any
difference is a defect in one of them. Deviation from correctly rounded ln is
a separate, measured question answered in test_fixed_point_ln -- this
operating point is FAITHFUL, not correctly rounded, and that is a fact about
the model, not a licence for the device to differ from it.

The powers of two are here for the same reason they are in the model's
sweep: normalisation is what a ln datapath gets wrong if it gets anything
wrong, and it fails at the exponent transitions or nowhere.
"""

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.ln_generator import LnTileGenerator
from dau_core.registry import loaded_cores
from dau_core.tests.cocotb_benches.rate_probe import assert_sustained_ingest

_MASK32 = 0xFFFF_FFFF
_INT32_MAX = (1 << 31) - 1
_ONE = 1 << 16  # 1.0 in Q16.16
_CORE = "fixed-point-ln"
# the datapath under test is the one the registry entry configures, so the
# bench cannot drift from the core it is benching
_GENERATOR = loaded_cores()[_CORE].generator
assert isinstance(_GENERATOR, LnTileGenerator)
_POINT = _GENERATOR.datapath

# The row-stream family's status codes. A non-positive input has no
# logarithm, so the tile takes the position the sqrt tile takes for a
# negative radicand (SqrtErrorCode.DOMAIN): invalidate the batch rather than
# emit a substituted number that looks right.
_ERR_DOMAIN = 3


def _boundary_values() -> list[int]:
    """Every 2**k and its neighbours: the exponent transitions."""
    values = []
    for shift in range(31):
        for delta in (-2, -1, 0, 1, 2):
            candidate = (1 << shift) + delta
            if 1 <= candidate <= _INT32_MAX:
                values.append(candidate)
    return values


@cocotb.test()
async def matches_the_model_on_directed_inputs(dut):
    """Values a human can check, so a wrong scale factor or a lost exponent
    cannot hide behind a random sweep."""
    await _start(dut)
    values = [_ONE, 2 * _ONE, 10 * _ONE, _ONE // 2, 1, _INT32_MAX]
    rows = [(index, value, 0, 0) for index, value in enumerate(values)]
    got = await _run(dut, rows)
    want = [(key, x, aux, _POINT.evaluate(x)) for key, x, aux, _ in rows]
    assert got == want, f"got {got}\nwant {want}"
    assert got[0][3] == 0, "ln(1.0) must be exactly zero"


@cocotb.test()
async def matches_the_model_across_the_domain(dut):
    await _start(dut)
    rng = random.Random(0x17)
    values = _boundary_values()
    values += [rng.randint(1, _INT32_MAX) for _ in range(120)]
    rows = [(index, value, 0, 0) for index, value in enumerate(values)]
    got = await _run(dut, rows)
    for (_, x, _, _), out in zip(rows, got):
        assert out[3] == _POINT.evaluate(x), f"x={x}: device {out[3]} != model {_POINT.evaluate(x)}"


@cocotb.test()
async def order_and_count_preserved_with_backpressure(dut):
    await _start(dut)
    values = [1, _ONE, 3 * _ONE, (1 << 30) - 1, _INT32_MAX, 7]
    rows = [(index, value, index + 1, 0) for index, value in enumerate(values)]
    got = await _run(dut, rows, ready_pattern=(1, 0, 0, 1, 0))
    want = [(key, x, aux, _POINT.evaluate(x)) for key, x, aux, _ in rows]
    assert got == want, f"got {got}\nwant {want}"


@cocotb.test()
async def non_positive_source_reports_domain_error(dut):
    """ln of zero or a negative has no value. The offending row must not come
    out with a substituted one, and the tile must recover for the next batch.

    status_ready is held low for longer than the pipeline is deep on purpose:
    when the close-out is taken promptly it flushes the pipeline anyway, so
    the guard on the push is only load-bearing under status backpressure.
    """
    await _start(dut)
    for bad in (0, -_ONE):
        await _reset(dut)
        rows = [(1, 4 * _ONE, 0, 0), (2, bad, 0, 0), (3, 9 * _ONE, 0, 0)]
        seen: list[int] = []
        drain = cocotb.start_soon(_drain_and_record(dut, seen))
        send = cocotb.start_soon(_send(dut, _pack(rows)))
        for _ in range(60):  # deeper than the pipeline
            await RisingEdge(dut.clk)
        emitted_keys = [_signed32(word & _MASK32) for word in seen[0::2]]
        assert 2 not in emitted_keys, f"the {bad} row was emitted with a substituted log: keys {emitted_keys}"

        await _expect_status(dut, code=_ERR_DOMAIN)
        await send
        drain.kill()
        dut.output_ready.value = 0

        # recovers: the next batch takes the log normally
        rows = [(index, value, 0, 0) for index, value in enumerate((_ONE, 2 * _ONE))]
        got = await _run(dut, rows)
        assert got == [(key, x, aux, _POINT.evaluate(x)) for key, x, aux, _ in rows]


@cocotb.test()
async def sustained_ingest_meets_registry_floor(dut):
    await _start(dut)
    rows = [(index, index * 7919 + 1, index + 2, 0) for index in range(256)]
    words = _pack(rows)
    await assert_sustained_ingest(
        dut,
        _CORE,
        send=lambda: _send(dut, words),
        input_beats=len(words),
        beats_per_row=2,
    )


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _pack(rows) -> list[int]:
    words = []
    for key, value, aux0, aux1 in rows:
        words.append(((value & _MASK32) << 32) | (key & _MASK32))
        words.append(((aux1 & _MASK32) << 32) | (aux0 & _MASK32))
    return words


async def _start(dut) -> None:
    # the clock is started ONCE per test. _reset re-arms the DUT between
    # cases: calling this in a loop would leave a second free-running Clock
    # driving dut.clk, and the result would depend on which driver the
    # simulator resolved last rather than on the tile.
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start(start_high=False))
    await _reset(dut)


async def _reset(dut) -> None:
    dut.rst.value = 1
    dut.cfg_dst_field.value = 3
    dut.cfg_src_field.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, *, ready_pattern=(1,)):
    words = _pack(rows)
    send = cocotb.start_soon(_send(dut, words))
    out_words = await _collect(dut, expect=len(words), ready_pattern=ready_pattern)
    await send
    out = []
    for i in range(0, len(out_words), 2):
        w0, w1 = out_words[i], out_words[i + 1]
        out.append((_signed32(w0 & _MASK32), _signed32(w0 >> 32), _signed32(w1 & _MASK32), _signed32(w1 >> 32)))
    return out


async def _send(dut, words) -> None:
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


async def _collect(dut, *, expect: int, ready_pattern=(1,)):
    """The proven quad-row collector: drive ready, sample in ReadOnly, and
    only count a beat that actually fired."""
    words: list[int] = []
    held = None
    for index in range(200000):
        ready = ready_pattern[index % len(ready_pattern)]
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
                assert len(words) == expect, f"expected {expect} words, got {len(words)}"
                return words
    raise AssertionError(f"timed out collecting ln output ({len(words)}/{expect})")


async def _drain_and_record(dut, seen: list[int]) -> None:
    """Drain the output, keeping what came out so a test can assert on it."""
    dut.output_ready.value = 1
    while True:
        await ReadOnly()
        if int(dut.output_valid.value) == 1:
            seen.append(int(dut.output_data.value))
        await RisingEdge(dut.clk)


async def _expect_status(dut, *, code: int) -> None:
    dut.status_ready.value = 1
    for _ in range(4096):
        await ReadOnly()
        fired = int(dut.status_valid.value) == 1
        observed_error = int(dut.status_error.value)
        observed_code = int(dut.status_error_code.value)
        await RisingEdge(dut.clk)
        if fired:
            dut.status_ready.value = 0
            assert observed_error == 1, "a close-out for a bad row must flag error"
            assert observed_code == code, f"status code {observed_code} != {code}"
            return
    raise AssertionError("timed out waiting for a status beat")

"""The rendered exp tile against the model it was rendered from.

Zero tolerance: the HDL mirrors ExpDatapath.evaluate step for step, so any
difference is a defect in one of them. Deviation from correctly rounded exp
is a separate, measured question answered in test_fixed_point_exp.
"""

from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.cores import loaded_cores
from dau_core.fixed_point import exp_domain

_MASK32 = 0xFFFF_FFFF
# the datapath under test is the one the registry entry configures, so the
# bench cannot drift from the core it is benching
# defaults to the exact point so the module imports standalone; the runner
# names the core under test
_POINT = loaded_cores()[os.environ.get("DAU_EXP_CORE", "int32-fixed-point-exp")].generator.datapath


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


@cocotb.test()
async def matches_the_model_on_directed_inputs(dut):
    await _start(dut)
    one = 1 << 16
    rows = [(i, x, 0, 0) for i, x in enumerate([0, one, -one, 2 * one, -2 * one, 5 * one, -5 * one])]
    got = await _run(dut, rows)
    want = [(k, x, a, _POINT.evaluate(x)) for k, x, a, _ in rows]
    assert got == want, f"got {got}\nwant {want}"


@cocotb.test()
async def matches_the_model_across_the_domain(dut):
    await _start(dut)
    low, high = exp_domain()
    rng = random.Random(0xE47)
    values = [low, low + 1, -1, 0, 1, high - 1, high]
    values += [rng.randint(low, high) for _ in range(120)]
    rows = [(i, v, 0, 0) for i, v in enumerate(values)]
    got = await _run(dut, rows)
    for (_, x, _, _), out in zip(rows, got):
        assert out[3] == _POINT.evaluate(x), f"x={x}: device {out[3]} != model {_POINT.evaluate(x)}"


async def _start(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start(start_high=False))
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
    words = []
    for key, value, aux0, aux1 in rows:
        words.append(((value & _MASK32) << 32) | (key & _MASK32))
        words.append(((aux1 & _MASK32) << 32) | (aux0 & _MASK32))
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
    only count a beat that actually fired. Sampling without checking the
    handshake is what made this harness report zeros against a tile the
    waveform showed was correct."""
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
    raise AssertionError(f"timed out collecting exp output ({len(words)}/{expect})")

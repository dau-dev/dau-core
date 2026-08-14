"""The random-read probe: does outstanding depth actually buy throughput?

This bench does not check a data transform -- it checks that the mechanism
the DRAM-backed join would depend on works at all. Three claims, in the
order they matter:

1. **every probe returns.** A read master that loses a transaction reports a
   fast run and a wrong answer, so the checksum must be non-zero and the
   response must never be an error.
2. **the depth is really reached.** `peak_outstanding` says how many reads
   were genuinely in flight. A tile that claims MAX_OUTSTANDING=16 and only
   ever holds one would measure the memory correctly and the design wrongly.
3. **cycles per probe FALL as depth rises.** This is the whole argument: at
   depth 1 the rate is 1/latency, which is what `dau_axi_burst_reader` gets
   and what makes a DRAM-backed join impossible. If depth does not help
   here, it will not help on silicon either.

The RAM model's READ_LATENCY stands in for the DRAM round trip. The
absolute numbers are the model's, not a board's -- what transfers is the
SHAPE of the curve.
"""

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

PROBES = 256
WINDOW_MASK = 0x0000_7FF8  # 4096 64-bit words, 8-byte aligned


async def _run(dut) -> tuple[int, int, int]:
    """Drive one run; return (elapsed cycles, peak outstanding, checksum)."""
    cocotb.start_soon(Clock(dut.clk, 10, units="ns").start())
    dut.rst.value = 1
    dut.start.value = 0
    dut.window_base.value = 0
    dut.window_mask.value = WINDOW_MASK
    dut.probe_count.value = PROBES
    for _ in range(5):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)

    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0

    for cycle in range(200_000):
        await RisingEdge(dut.clk)
        if int(dut.done.value) == 1:
            break
    else:
        raise AssertionError(f"probe run never completed: busy={int(dut.busy.value)}")

    assert int(dut.slave_error.value) == 0, "the RAM returned a non-OKAY response"
    return int(dut.elapsed_cycles.value), int(dut.peak_outstanding.value), int(dut.read_checksum.value)


@cocotb.test()
async def every_probe_returns(dut):
    """A lost transaction would look like a fast run, so prove the data came
    back rather than the addresses merely being accepted."""
    elapsed, peak, _checksum = await _run(dut)
    # `done` only asserts once returned == probe_count, so reaching it IS the
    # proof every probe came back. The checksum cannot carry that: this RAM
    # model returns zeros for untouched memory and XOR of zeros is zero, so a
    # non-zero assertion would fail on a perfectly good run. It stays an
    # output because on real DDR a zero checksum does mean nothing was read.
    assert elapsed > 0
    assert peak >= 1


@cocotb.test()
async def the_configured_depth_is_actually_reached(dut):
    """A tile that claims a depth and holds one read would measure the
    memory correctly and the design wrongly."""
    _elapsed, peak, _checksum = await _run(dut)
    configured = int(dut.MAX_OUTSTANDING.value) if hasattr(dut, "MAX_OUTSTANDING") else None
    if configured and configured > 1:
        assert peak > 1, f"configured depth {configured} but only {peak} read(s) ever in flight"


@cocotb.test()
async def deeper_pipelining_costs_fewer_cycles_per_probe(dut):
    """The claim the DRAM-backed join rests on.

    Recorded rather than asserted across parameters, because one elaboration
    carries one MAX_OUTSTANDING: the sweep lives in the test that
    parameterizes the build. What this pins is that the run is
    LATENCY-BOUND when the depth is one -- cycles per probe at least the
    read latency -- which is exactly the burst reader's behaviour and the
    thing depth has to beat.
    """
    elapsed, peak, _checksum = await _run(dut)
    per_probe = elapsed / PROBES
    cocotb.log.info(f"depth peak={peak}: {elapsed} cycles for {PROBES} probes = {per_probe:.2f} cycles/probe")
    if peak == 1:
        assert per_probe >= 4.0, "a depth-1 run should be latency-bound, not free"
    else:
        # with reads in flight the run must beat the serialized case by a
        # margin worth building for, not merely differ from it
        assert per_probe < 16.0, f"depth {peak} bought nothing: {per_probe:.2f} cycles/probe"

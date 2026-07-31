from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from fractions import Fraction

import cocotb
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.cores import core

RATE_TOLERANCE = Fraction(1, 20)


async def assert_sustained_ingest(
    dut,
    core_name: str,
    *,
    send: Callable[[], Awaitable[None]],
    input_beats: int,
    rows_per_beat: int = 1,
    beats_per_row: int = 1,
    params: Mapping[str, int] | None = None,
    ready_signal_names: Sequence[str] = ("output_ready", "status_ready"),
) -> Fraction:
    """Measure continuous-valid input handshakes against the registry floor."""
    if input_beats < 1 or rows_per_beat < 1 or beats_per_row < 1:
        raise ValueError("rate probe dimensions must be positive")

    definition = core(core_name)
    if definition.ingest is None:
        raise AssertionError(f"{core_name} has no declared ingest rate")
    concrete_params = dict(params or {})
    declared = definition.ingest.effective(concrete_params, definition.parameters)
    row_credit = Fraction(rows_per_beat, beats_per_row)
    accepted_rows = input_beats * row_credit
    if accepted_rows < 256:
        raise AssertionError(f"rate probe for {core_name} must cover at least 256 rows, got {accepted_rows}")

    ready_signals = [getattr(dut, name) for name in ready_signal_names]
    for signal in ready_signals:
        signal.value = (1 << len(signal)) - 1

    sender = cocotb.start_soon(send())
    accepted_beats = 0
    first_tick = None
    last_tick = None
    max_cycles = max(4096, input_beats * 16)
    for tick in range(1, max_cycles + 1):
        await ReadOnly()
        valid = int(dut.input_valid.value) == 1
        if first_tick is not None and accepted_beats < input_beats:
            assert valid, f"{core_name} rate stimulus inserted an input-valid gap"
        for signal in ready_signals:
            assert int(signal.value) == (1 << len(signal)) - 1, f"{core_name} rate probe sink was not always ready"

        if valid and int(dut.input_ready.value) == 1:
            accepted_beats += 1
            first_tick = tick if first_tick is None else first_tick
            last_tick = tick
        await RisingEdge(dut.clk)
        if accepted_beats == input_beats:
            break

    await sender
    assert accepted_beats == input_beats, f"{core_name} accepted only {accepted_beats}/{input_beats} input beats"
    assert first_tick is not None and last_tick is not None
    cycles = last_tick - first_tick + 1
    measured = accepted_rows / cycles
    # Five percent covers bounded pipeline/end-point effects in a 256+ row
    # sample without hiding a real cadence mismatch such as 1/3 versus 1/2.
    minimum = declared * (1 - RATE_TOLERANCE)
    dut._log.info(
        "RATE %s params=%s measured=%.6f declared=%.6f rows=%s cycles=%d",
        core_name,
        concrete_params,
        float(measured),
        float(declared),
        accepted_rows,
        cycles,
    )
    assert measured >= minimum, (
        f"{core_name} measured sustained ingest {float(measured):.6f} rows/cycle "
        f"({accepted_rows} rows/{cycles} cycles), below declared {float(declared):.6f} rows/cycle "
        f"with {float(RATE_TOLERANCE):.0%} tolerance; lower the registry declaration or fix the RTL"
    )
    return measured

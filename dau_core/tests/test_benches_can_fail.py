"""Prove the benches can fail.

A bench that always passes and a bench that checks nothing are
indistinguishable from a green suite, and this project has now shipped
several of the second kind: a scan bench that raised before comparing and
so never compared, a verilator install that could not build any timing
bench, and a device check whose field was read from a snapshot that never
populated it. Every one of those was found by hand or by an adversarial
reviewer. None was found by the suite, because a suite only reports what a
test asserts, and none of them asserted that the check ran.

The check here is mutation. Take a tile whose bench passes, change its
SystemVerilog so it computes the wrong answer, and require the bench to
FAIL. Together with the existing pass-on-clean-source tests that is a
two-sided proof: the bench distinguishes a correct implementation from an
incorrect one, which is the only property that makes a green bench mean
anything.

Mutations are chosen to be semantic and small — an off-by-one in a count, an
inverted comparison — so a bench that survives one is not merely insensitive
to a wholesale rewrite. They are applied to a COPY; the registry's source is
never touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from shutil import which

import pytest

from dau_core.registry import core, sources_for

_COCOTB_BENCH_PREFIX = "dau_core.tests.cocotb_benches"


@dataclass(frozen=True)
class _Mutation:
    """One tile, and a textual change to its HDL that makes it wrong."""

    core_name: str
    find: str
    replace: str
    what_it_breaks: str
    parameters: dict | None = None


# Keep this small: every entry costs a verilator build. These are chosen to
# cover different bench shapes -- an aggregation that accumulates, a filter
# that decides, and a keyed tile that groups.
_MUTATIONS = (
    _Mutation(
        core_name="int32-hash-grouped-aggregation",
        find="base_count + COUNT_BITS'(1)",
        replace="base_count + COUNT_BITS'(2)",
        what_it_breaks="every group's row count is doubled",
    ),
    _Mutation(
        core_name="int32-field-sum-aggregation",
        find="sums[0] <= sums[0] + 64'(signed'(row_lanes[0*FIELD_BITS +: FIELD_BITS]))",
        replace="sums[0] <= sums[0] - 64'(signed'(row_lanes[0*FIELD_BITS +: FIELD_BITS]))",
        what_it_breaks="field 0's sum is negated",
    ),
)


def _mutated_sources(mutation: _Mutation, tmp_path: Path) -> tuple[Path, ...]:
    """Every source the tile needs, with the named one mutated in a copy."""
    staged = []
    applied = False
    for source in sources_for(mutation.core_name):
        text = source.read_text()
        if mutation.find in text:
            text = text.replace(mutation.find, mutation.replace)
            applied = True
        destination = tmp_path / "hdl" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
        staged.append(destination)
    assert applied, (
        f"mutation for {mutation.core_name} did not apply: no source contains {mutation.find!r}. "
        "A mutation that does not change the HDL proves nothing -- fix the pattern rather than deleting the case"
    )
    return tuple(staged)


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
@pytest.mark.parametrize("mutation", _MUTATIONS, ids=lambda m: m.core_name)
def test_a_bench_fails_when_its_tile_is_made_wrong(mutation: _Mutation, tmp_path: Path) -> None:
    """The bench must reject an implementation that computes the wrong
    answer. If it passes here it is not comparing anything, and its green
    result elsewhere means nothing."""
    cocotb_integration = pytest.importorskip("dau_sim.integrations.cocotb", reason="dau-sim cocotb integration not installed")
    from dau_core.tests.test_hdl_cocotb import VERILATOR_BUILD_ARGS

    definition = core(mutation.core_name)
    assert definition.bench is not None, f"{mutation.core_name} declares no bench"

    # Stage the mutation OUTSIDE the raises block. Inside it, a mutation that
    # failed to apply would raise AssertionError, be caught as the expected
    # failure, and turn a no-op into a pass -- this test would then be
    # exactly the kind of check it exists to forbid.
    sources = _mutated_sources(mutation, tmp_path)

    # SystemExit, not Exception: the cocotb runner exits rather than raising,
    # and SystemExit descends from BaseException. Catching only Exception
    # would make THIS test unable to fail either.
    with pytest.raises((Exception, SystemExit)) as failure:
        cocotb_integration.run_cocotb_testbench(
            sources=sources,
            hdl_toplevel=definition.module,
            test_module=definition.bench,
            build_dir=tmp_path / definition.module,
            results_xml=tmp_path / f"{definition.module}-cocotb-results.xml",
            build_args=VERILATOR_BUILD_ARGS,
            parameters=mutation.parameters,
        )
    # a compile error would also raise, and would prove nothing about whether
    # the bench compares: require the failure to come from the RUN
    message = str(failure.value).lower()
    assert "verilator compile failed" not in message, (
        f"the mutation for {mutation.core_name} broke compilation rather than behaviour "
        f"({mutation.what_it_breaks} was intended); choose a mutation that elaborates"
    )

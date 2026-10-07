"""The exp datapath's accuracy is a measured fact, and this is the measurement.

Two layers are checked separately, which is the whole accuracy contract:

* the ALGORITHM against the MATHEMATICS, here, by sweeping inputs; and
* the HDL against the ALGORITHM, in the cocotb bench, with zero tolerance.

The default sweep is strided so the suite stays quick. The exhaustive sweep
is the one that actually licenses the claim and runs under DAU_EXHAUSTIVE=1 --
because a stride is a sample, and a sample declared Q48/degree-5 flawless
when the full sweep found three wrong inputs.
"""

from __future__ import annotations

import math
import os

import pytest

from dau_core.fixed_point import ExpDatapath, exp_domain, exp_reference
from dau_core.registry import core

_EXHAUSTIVE = os.environ.get("DAU_EXHAUSTIVE") == "1"
_STRIDE = 1 if _EXHAUSTIVE else 149


def _worst_deviation(point: ExpDatapath, *, stride: int) -> tuple[int, int, int]:
    """(worst ULP, wrong count, checked count) against the correctly rounded
    reference. Double decides the ordinary inputs; the few that sit too close
    to a rounding boundary for it are re-decided in 80 digits."""
    low, high = exp_domain(fractional_bits=point.fractional_bits)
    one = 1 << point.fractional_bits
    worst = wrong = checked = 0
    for x in range(low, high + 1, stride):
        approximate = math.exp(x / one) * one + 0.5
        want = exp_reference(x) if abs(approximate - round(approximate)) < 1e-5 else math.floor(approximate)
        delta = abs(point.evaluate(x) - want)
        checked += 1
        if delta:
            wrong += 1
            worst = max(worst, delta)
    return worst, wrong, checked


def test_the_reference_is_the_mathematics_not_an_algorithm() -> None:
    """Spot values a human can verify, so the reference cannot drift into
    being a description of whatever the datapath happens to do."""
    one = 1 << 16
    assert exp_reference(0) == one
    assert exp_reference(one) == round(math.e * one)
    assert exp_reference(-one) == round(math.exp(-1) * one)
    # correctly rounded means nearest, so the neighbours must differ by one
    assert exp_reference(one) - exp_reference(one - 1) in (0, 1, 2)


def test_exp_overflowing_int32_is_refused_not_wrapped() -> None:
    _, high = exp_domain()
    exp_reference(high)  # the largest input that still fits
    with pytest.raises(ValueError, match="does not fit int32"):
        exp_reference(high + 1)


EXP_CORES = ("fixed-point-exp", "fixed-point-exp-light")


def _point(name: str) -> ExpDatapath:
    """The registered operating point: the exp tile's generator datapath."""
    generator = core(name).generator
    assert generator is not None, f"{name} is a generated tile"
    datapath = generator.datapath
    assert isinstance(datapath, ExpDatapath)
    return datapath


def _recorded_ulp(name: str) -> int:
    recorded = core(name).accuracy_ulp
    assert recorded is not None, f"{name} records no accuracy_ulp"
    return recorded


@pytest.mark.parametrize("name", EXP_CORES)
def test_each_operating_point_meets_its_recorded_bound(name: str) -> None:
    """The registered bound is what the registry and the docs quote. If a
    point drifts past it, that is a change to an advertised fact."""
    worst, wrong, checked = _worst_deviation(_point(name), stride=_STRIDE)
    recorded = _recorded_ulp(name)
    assert worst <= recorded, (
        f"{name} deviates by {worst} ULP over {checked:,} inputs ({wrong:,} wrong), "
        f"but the registry records accuracy_ulp {recorded}. Re-measure exhaustively and update the record, "
        "or fix the datapath"
    )


def test_the_exact_point_is_correctly_rounded() -> None:
    """The stronger claim, kept separate because it is the one that makes the
    contract mathematical rather than a description of our polynomial."""
    worst, wrong, checked = _worst_deviation(_point("fixed-point-exp"), stride=_STRIDE)
    assert (worst, wrong) == (0, 0), f"the exact point is not correctly rounded: {wrong:,} of {checked:,} inputs, worst {worst} ULP"


def test_the_light_point_is_actually_lighter() -> None:
    """A second operating point only earns its place by costing less. Guard
    the reason it exists, not just its accuracy."""
    exact, light = _point("fixed-point-exp"), _point("fixed-point-exp-light")
    assert light.internal_bits < exact.internal_bits
    assert light.multiplies <= exact.multiplies
    assert _recorded_ulp("fixed-point-exp-light") > _recorded_ulp("fixed-point-exp"), "a point that is no less accurate is not a trade-off"


def test_rounding_steps_only_help_where_precision_binds() -> None:
    """Pins the finding that decided the operating points: below ~40 bits the
    internal precision binds and rounding each Horner step helps; above it the
    polynomial degree binds and rounding changes nothing. Getting this
    backwards sends you adding precision to a problem that needs degree."""
    coarse = ExpDatapath(internal_bits=32, table_bits=8, degree=3)
    coarse_rounded = coarse.model_copy(update={"round_steps": True})
    assert _worst_deviation(coarse_rounded, stride=_STRIDE)[1] <= _worst_deviation(coarse, stride=_STRIDE)[1]

    fine = ExpDatapath(internal_bits=52, table_bits=8, degree=4)
    fine_rounded = fine.model_copy(update={"round_steps": True})
    assert _worst_deviation(fine, stride=_STRIDE)[0] == _worst_deviation(fine_rounded, stride=_STRIDE)[0] == 0


def test_the_range_reduction_index_is_deliberately_narrow() -> None:
    """`k` picks the split between table index and residue, nothing more.

    Computing it at full internal precision made it the WIDEST multiply in
    the datapath -- a 48-bit operand against a 61-bit constant, measured at 9
    DSP48s -- and bought no accuracy: the sweep is correctly rounded at every
    width down to 12 bits, because a k off by one merely widens the residue
    and the polynomial still covers it.

    Widening it back is not wrong, only wasteful, so this guards the reason
    rather than the number: more precision here must not change a single
    result.
    """
    exact = _point("fixed-point-exp")
    assert exact.k_bits <= 32, "the reduction index does not need full internal precision"

    wide = exact.model_copy(update={"k_bits": 52})
    low, high = exp_domain()
    for x in range(low, high + 1, _STRIDE):
        assert exact.evaluate(x) == wide.evaluate(x), f"widening k changed the result at x={x}"

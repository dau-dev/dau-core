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

from dau_core.fixed_point import EXP_EXACT, EXP_LIGHT, EXP_MEASURED_ULP, ExpDatapath, exp_domain, exp_reference

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


@pytest.mark.parametrize(("name", "point"), [("exact", EXP_EXACT), ("light", EXP_LIGHT)])
def test_each_operating_point_meets_its_recorded_bound(name: str, point: ExpDatapath) -> None:
    """The registered bound is what the registry and the docs quote. If a
    point drifts past it, that is a change to an advertised fact."""
    worst, wrong, checked = _worst_deviation(point, stride=_STRIDE)
    recorded = EXP_MEASURED_ULP[name]
    assert worst <= recorded, (
        f"{name} point deviates by {worst} ULP over {checked:,} inputs ({wrong:,} wrong), "
        f"but EXP_MEASURED_ULP records {recorded}. Re-measure exhaustively and update the record, "
        "or fix the datapath"
    )


def test_the_exact_point_is_correctly_rounded() -> None:
    """The stronger claim, kept separate because it is the one that makes the
    contract mathematical rather than a description of our polynomial."""
    worst, wrong, checked = _worst_deviation(EXP_EXACT, stride=_STRIDE)
    assert (worst, wrong) == (0, 0), f"the exact point is not correctly rounded: {wrong:,} of {checked:,} inputs, worst {worst} ULP"


def test_the_light_point_is_actually_lighter() -> None:
    """A second operating point only earns its place by costing less. Guard
    the reason it exists, not just its accuracy."""
    assert EXP_LIGHT.internal_bits < EXP_EXACT.internal_bits
    assert EXP_LIGHT.multiplies <= EXP_EXACT.multiplies
    assert EXP_MEASURED_ULP["light"] > EXP_MEASURED_ULP["exact"], "a point that is no less accurate is not a trade-off"


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

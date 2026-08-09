"""The normal CDF datapath, swept over EVERY representable input.

This is the one transcendental whose useful domain is small enough that the
correctness claim is a proof rather than a sample: 573,441 inputs, all of
them checked here by default. exp is 1.45M (feasible but slower, so strided
by default) and ln is 2.1 billion (never exhaustive in the suite).
"""

from __future__ import annotations

import math

import pytest

from dau_core.fixed_point import NormalCdfDatapath, normal_cdf_domain, normal_cdf_reference

CDF_EXACT = NormalCdfDatapath()
#: Exhaustively measured deviation from the reference, in ULP.
CDF_MEASURED_ULP = 0


def _sweep(point: NormalCdfDatapath) -> tuple[int, int, int]:
    """(worst ULP, wrong, checked) over the whole domain. Double decides the
    ordinary inputs; anything too close to a rounding boundary for it is
    re-decided against the 80-digit reference."""
    low, high = normal_cdf_domain()
    one = 1 << point.fractional_bits
    worst = wrong = checked = 0
    for x in range(low, high + 1):
        approximate = 0.5 * math.erfc(-(x / one) / math.sqrt(2)) * one + 0.5
        want = normal_cdf_reference(x) if abs(approximate - round(approximate)) < 1e-6 else math.floor(approximate)
        delta = abs(point.evaluate(x) - want)
        checked += 1
        if delta:
            wrong += 1
            worst = max(worst, delta)
    return worst, wrong, checked


def test_the_registered_point_is_correctly_rounded_over_every_input() -> None:
    worst, wrong, checked = _sweep(CDF_EXACT)
    assert checked == 573441, f"the domain changed: {checked:,} inputs"
    assert (worst, wrong) == (CDF_MEASURED_ULP, 0), f"{wrong:,} of {checked:,} inputs wrong, worst {worst} ULP"


def test_the_tile_is_total_and_saturates_exactly() -> None:
    """No error path: every int32 input has an answer, and the saturation IS
    the correctly rounded value rather than a clamp."""
    low, high = normal_cdf_domain()
    one = 1 << 16
    assert CDF_EXACT.evaluate(high) == one
    assert CDF_EXACT.evaluate(low) == 0
    for far in (1 << 30, (1 << 31) - 1):
        assert CDF_EXACT.evaluate(far) == one
        assert CDF_EXACT.evaluate(-far) == 0


def test_symmetry_holds_exactly() -> None:
    """The datapath folds on it, so a break here is a wrong answer for every
    negative input at once."""
    one = 1 << 16
    for magnitude in range(0, 4 * one, 1021):
        assert CDF_EXACT.evaluate(magnitude) + CDF_EXACT.evaluate(-magnitude) == one


@pytest.mark.parametrize(("table_bits", "degree"), [(8, 3)])
def test_a_cheaper_point_is_not_correctly_rounded(table_bits: int, degree: int) -> None:
    """Pins WHY the registered point costs what it does: the obvious cheaper
    configuration misses, so the extra segments are load-bearing rather than
    conservative."""
    worst, wrong, _ = _sweep(NormalCdfDatapath(table_bits=table_bits, degree=degree))
    assert wrong > 0 and worst >= 1, "the cheaper point now passes -- re-measure and lower the registered one"

"""The ln datapath's accuracy is a measured fact, like exp's.

ln's domain is 2,147,483,647 inputs -- exhaustive in this model but not in
simulation, so the default sweep is strided and DAU_EXHAUSTIVE=1 runs the
whole thing. The strided sweep always includes every power-of-two boundary,
because those are the exponent transitions where a normalizing datapath
fails if it fails anywhere.
"""

from __future__ import annotations

import os
import random

import pytest

from dau_core.fixed_point import LnDatapath, ln_reference

_EXHAUSTIVE = os.environ.get("DAU_EXHAUSTIVE") == "1"
LN_EXACT = LnDatapath()
#: Exhaustively measured deviation from ln_reference, in ULP.
LN_MEASURED_ULP = 0


def _boundary_inputs() -> list[int]:
    """Every 2**k and its neighbours: the exponent transitions."""
    cases = []
    for shift in range(31):
        for delta in (-2, -1, 0, 1, 2):
            candidate = (1 << shift) + delta
            if 1 <= candidate <= (1 << 31) - 1:
                cases.append(candidate)
    return cases


def _sweep(point: LnDatapath, values) -> tuple[int, int, int]:
    worst = wrong = checked = 0
    for x in values:
        delta = abs(point.evaluate(x) - ln_reference(x))
        checked += 1
        if delta:
            wrong += 1
            worst = max(worst, delta)
    return worst, wrong, checked


def test_the_reference_is_the_mathematics_not_an_algorithm() -> None:
    """Values a human can check, so the reference cannot drift into being a
    description of whatever the datapath does."""
    import math

    one = 1 << 16
    assert ln_reference(one) == 0
    assert ln_reference(2 * one) == round(math.log(2) * one)
    assert ln_reference(10 * one) == round(math.log(10) * one)


def test_ln_of_a_non_positive_value_is_refused() -> None:
    """Same position the sqrt tile takes: no value exists, and substituting
    one would be a wrong number that looks right."""
    for bad in (0, -1, -(1 << 16)):
        with pytest.raises(ValueError, match="non-positive"):
            ln_reference(bad)
    with pytest.raises(ValueError, match="positive"):
        LN_EXACT.evaluate(0)


def test_the_exponent_boundaries_are_exact() -> None:
    """Normalisation is the part that can be off by one power of two, and it
    shows up here or nowhere."""
    worst, wrong, checked = _sweep(LN_EXACT, _boundary_inputs())
    assert (worst, wrong) == (0, 0), f"{wrong} of {checked} boundary inputs wrong, worst {worst} ULP"


def test_the_operating_point_meets_its_recorded_bound() -> None:
    rng = random.Random(0x1A7)
    values = _boundary_inputs()
    values += range(1, (1 << 31) - 1) if _EXHAUSTIVE else [rng.randint(1, (1 << 31) - 1) for _ in range(20000)]
    worst, wrong, checked = _sweep(LN_EXACT, values)
    assert worst <= LN_MEASURED_ULP, (
        f"ln deviates by {worst} ULP over {checked:,} inputs ({wrong:,} wrong), but LN_MEASURED_ULP records "
        f"{LN_MEASURED_ULP}. Re-measure exhaustively and update the record, or fix the datapath"
    )

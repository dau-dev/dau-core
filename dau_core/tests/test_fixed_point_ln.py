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
# Deviation from ln_reference, in ULP, measured by the EXHAUSTIVE sweep
# over all 2,147,483,647 positive inputs -- not by the strided one.
#:
# MEASURED 2026-08-09, whole domain: 459 of 2,147,483,647 inputs off by
# one, worst 1 ULP, first at x=3290838. A rate of 2.1e-7 -- which is exactly
# why the 60,000-sample sweep that preceded it recorded 0 and was wrong.
# FAITHFUL, not correctly rounded.
#:
# Third time in this arc that a sample declared a datapath flawless and the
# full sweep disagreed (Q48/degree-5 exp, then the 256-segment CDF). At this
# rate no stratified sample would ever have found it: the bound is only ever
# what was actually swept.
#:
# 4,397 inputs sat too close to a rounding boundary for double to decide and
# were re-decided against the 80-digit reference.
LN_MEASURED_ULP = 1


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


def test_ln_is_faithful_not_correctly_rounded() -> None:
    """Records the class honestly. exp-light and the normal CDF reach their
    registered contracts; ln does not at this operating point, and a caller
    choosing between them should see that rather than infer it.

    Whether a correctly-rounded ln is worth having is a separate question:
    the exp arc showed the fix is either more internal precision or more
    polynomial degree, and which one binds is not predictable without
    measuring both.
    """
    assert LN_MEASURED_ULP == 1


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


def test_the_two_tier_sweep_agrees_with_the_exact_reference() -> None:
    """The affordable sweep must answer what the expensive one answers.

    The harness decides in double and re-decides only near-boundary inputs
    against the 80-digit reference. That is only sound if it never differs
    from an all-exact sweep, so this checks both over a window containing a
    KNOWN deviation -- a harness that agreed everywhere except where the
    datapath is wrong would be worse than useless.
    """
    from dau_core.tests.exhaustive_sweep import sweep

    window = range(3290838 - 3000, 3290838 + 3000)
    fast = sweep(LN_EXACT.evaluate, ln_reference, window, scale=1 << 16)
    exact_worst, exact_wrong, _ = _sweep(LN_EXACT, window)
    assert (fast.worst, fast.wrong) == (exact_worst, exact_wrong)
    assert fast.first == 3290838, fast
    assert fast.faithful and not fast.correctly_rounded


def test_the_light_point_is_faithful_at_the_full_points_known_deviation() -> None:
    """The registered light point carries the SAME 1-ULP contract, measured
    by an exhaustive sweep (3,544 of 2,147,483,647 off by one, against the
    full point's 459). Pin the shared boundary case here; the full-domain
    number lives in the registry entry and BENCHMARKS."""
    from dau_core.fixed_point import LnDatapath

    light = LnDatapath(internal_bits=36)
    assert abs(light.evaluate(3290838) - ln_reference(3290838)) <= 1

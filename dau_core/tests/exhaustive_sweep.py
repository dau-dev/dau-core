"""Exhaustive accuracy sweeps for the fixed-point transcendental datapaths.

A bound is only ever what was actually swept. This arc has four recorded
cases of a sample declaring a datapath flawless that the full domain
disproved -- Q48/degree-5 exp, the 256-segment CDF, ln at 60,000 samples,
and ln again at 663,000 -- so a registered accuracy fact comes from here,
not from a stratified sample.

THE COST IS THE REFERENCE, NOT THE DATAPATH. `ln_reference` and its
siblings compute in 80 decimal digits so that half-way cases double cannot
decide are decided correctly. Calling that for every input puts a full
2**31 sweep in the tens of core-hours (an estimate from the per-input cost
when this was written, not a timed run). Deciding in double and re-deciding
only the inputs that sit too close to a rounding boundary brings it down to
minutes on a workstation, because only a few thousand inputs are close.
Both answers are identical; only one is affordable enough to run on every
candidate operating point.

This module was written after the slow estimate had sent a candidate light
point to the back of the queue as a multi-session task. It is committed so
the capability is not rediscovered a third time.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable

# how close to a .5 boundary a double result must be before the exact
# reference is consulted. Generous on purpose: a false positive costs one
# Decimal evaluation, a false negative costs a wrong bound.
BOUNDARY_GUARD = 1e-6


class SweepResult:
    """What an exhaustive sweep establishes about one operating point."""

    def __init__(self, worst: int, wrong: int, checked: int, first: int | None, boundary: int) -> None:
        self.worst, self.wrong, self.checked, self.first, self.boundary = worst, wrong, checked, first, boundary

    @property
    def faithful(self) -> bool:
        """Never more than one ULP from the correctly rounded answer."""
        return self.worst <= 1

    @property
    def correctly_rounded(self) -> bool:
        return self.worst == 0

    def __repr__(self) -> str:
        return (
            f"SweepResult(worst={self.worst}, wrong={self.wrong:,} of {self.checked:,}, first={self.first}, boundary_redecisions={self.boundary:,})"
        )


def sweep(
    evaluate: Callable[[int], int],
    reference: Callable[[int], int],
    values: Iterable[int],
    *,
    scale: int,
    transform: Callable[[float], float] = math.log,
    guard: float = BOUNDARY_GUARD,
) -> SweepResult:
    """Compare a datapath against its mathematical reference over ``values``.

    ``transform`` is the real-valued function in double precision; the
    exact ``reference`` is consulted only where double cannot decide the
    rounding. The two-tier split is what makes a full-domain sweep
    affordable -- see the module docstring.
    """
    worst = wrong = checked = boundary = 0
    first: int | None = None
    for x in values:
        checked += 1
        got = evaluate(x)
        approx = transform(x / scale) * scale
        frac = approx + 0.5
        if abs(frac - round(frac)) < guard:
            boundary += 1
            want = reference(x)
        else:
            want = math.floor(frac)
        delta = abs(got - want)
        if delta:
            wrong += 1
            if first is None:
                first = x
            worst = max(worst, delta)
    return SweepResult(worst, wrong, checked, first, boundary)

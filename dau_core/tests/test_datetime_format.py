"""The datetime formatter's golden, and the constants the tile divides by.

The tile has no divider: every division in it is a multiply-shift by a
compile-time constant, and a multiply-shift identity is only valid over a
bounded input range. Those bounds are the whole correctness argument for the
arithmetic, and they live in the HDL as bare magic numbers -- so this reads
them back out of the .sv and proves each one against real floor division at
BOTH ENDS of every quotient block, which is where such an identity fails
first. A constant edited in the HDL without its range being re-derived fails
here rather than in a rendered date.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dau_core.hdl import DAU_DATETIME_FORMAT_SV
from dau_core.operators import (
    DATETIME_FORMAT_MAX_EPOCH_SECONDS,
    DATETIME_FORMAT_WIDTH,
    DatetimeFormatErrorCode,
    format_datetime,
    format_datetime_rows,
)

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# (localparam name, pre-shift, divisor, largest input the tile can present).
# The bound is what the stage upstream proves, not a guess: day-of-era is at
# most 146096 because the era subtraction just removed 146097 per era, and so
# on down the chain.
MULTIPLY_SHIFT_SITES = (
    ("DAY", 7, 86400, DATETIME_FORMAT_MAX_EPOCH_SECONDS),
    ("HOUR", 4, 3600, 86399),
    ("MIN", 2, 60, 3599),
    ("ERA", 0, 146097, 3652364),
    ("1460", 2, 1460, 146096),
    ("36524", 2, 36524, 146096),
    ("146096", 4, 146096, 146096),
    ("365", 0, 365, 146096),
    ("100", 2, 100, 399),
    ("153", 0, 153, 1827),
    ("5", 0, 5, 1685),
)


def _hdl_constants() -> dict[str, int]:
    text = Path(str(DAU_DATETIME_FORMAT_SV)).read_text()
    found: dict[str, int] = {}
    for name, value in re.findall(r"localparam\s+(?:logic\s*\[[^\]]+\]|int)\s+(MAGIC_\w+|SHIFT_\w+|PRE_\w+)\s*=\s*(?:\d+')?d?(\d+)", text):
        found[name] = int(value)
    return found


@pytest.mark.parametrize(("site", "pre_shift", "divisor", "max_input"), MULTIPLY_SHIFT_SITES)
def test_the_tiles_multiply_shift_constants_are_exact_over_their_declared_range(site: str, pre_shift: int, divisor: int, max_input: int) -> None:
    constants = _hdl_constants()
    magic = constants[f"MAGIC_{site}"]
    shift = constants[f"SHIFT_{site}"]
    assert constants.get(f"PRE_{site}", pre_shift) == pre_shift, f"{site}: HDL pre-shift disagrees"

    quotient = 0
    while True:
        low = quotient * divisor << pre_shift
        if low > max_input:
            break
        # the top of the block is the last input that must still floor here;
        # the pre-shift means every value in [low, high] shares one quotient
        high = min(((quotient + 1) * divisor << pre_shift) - 1, max_input)
        for value in (low, high):
            assert ((value >> pre_shift) * magic) >> shift == value // divisor, f"{site} fails at {value}"
        quotient += 1


def test_the_golden_agrees_with_the_standard_library_at_the_boundaries() -> None:
    interesting = [
        0,
        1,
        59,
        60,
        3599,
        3600,
        86399,
        86400,
        951782400,  # 2000-02-29, the century that IS a leap year
        951868799,
        4107456000,  # 2100-02-28, the century that is NOT
        4107542400,
        1709164800,  # 2024-02-29
        DATETIME_FORMAT_MAX_EPOCH_SECONDS,
    ]
    for value in interesting:
        expected = (EPOCH + timedelta(seconds=value)).strftime("%Y-%m-%d %H:%M:%S")
        assert format_datetime(value) == expected, value
        assert len(format_datetime(value)) == DATETIME_FORMAT_WIDTH


def test_the_golden_agrees_with_the_standard_library_over_a_random_sweep() -> None:
    rng = random.Random(7)
    values = [rng.randrange(0, DATETIME_FORMAT_MAX_EPOCH_SECONDS + 1) for _ in range(5000)]
    expected = [(EPOCH + timedelta(seconds=value)).strftime("%Y-%m-%d %H:%M:%S") for value in values]
    assert format_datetime_rows(values) == expected


def test_every_day_of_one_leap_year_and_one_common_year_renders() -> None:
    """Leap handling is not special-cased anywhere -- it falls out of the
    400-year era arithmetic -- so the way to check it is to walk the days."""
    for year in (2024, 2023):
        start = int((datetime(year, 1, 1, tzinfo=UTC) - EPOCH).total_seconds())
        for day in range(366 if year == 2024 else 365):
            value = start + day * 86400
            assert format_datetime(value) == (EPOCH + timedelta(seconds=value)).strftime("%Y-%m-%d %H:%M:%S")


def test_out_of_range_epochs_have_no_rendering() -> None:
    for value in (-1, DATETIME_FORMAT_MAX_EPOCH_SECONDS + 1):
        with pytest.raises(ValueError, match="outside"):
            format_datetime(value)
    # the tile's wire encoding for the same case
    assert DatetimeFormatErrorCode.DOMAIN == 3

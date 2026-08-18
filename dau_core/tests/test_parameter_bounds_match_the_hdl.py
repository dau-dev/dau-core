"""A parameter bound stated twice is a bound that can disagree with itself.

The registry declares what a tile's parameters may be; the tile's own
elaboration guard declares the same thing in SystemVerilog, because Verilog
cannot read the registry. Both are necessary. Nothing made them agree, so
the registry could offer a K the tile refuses to elaborate, or -- worse --
accept one the tile elaborates into something unmeasured.

The tiles state their bounds in a small set of conventional forms, which is
what makes this checkable. A form this cannot parse is listed explicitly
rather than skipped silently: an unparsed guard is an unchecked guard, and a
holdout list that nobody can see is how the last audit went stale.
"""

from __future__ import annotations

import re
from pathlib import Path

from dau_core import hdl
from dau_core.element_types import decode_element_type
from dau_core.registry import loaded_cores

_HDL_DIR = Path(str(hdl.__path__[0]))

# Elaboration guards whose wording states a range: (minimum, maximum).
_RANGE_FORMS = (
    re.compile(r"(\w+) must be in \[(-?\d+), (-?\d+)\]"),
    re.compile(r"(\w+) must be between (-?\d+) and (-?\d+)"),
    re.compile(r"(\w+) must divide \d+ and lie in \[(-?\d+), (-?\d+)\]"),
)
# Guards whose wording enumerates the legal values.
_CHOICE_FORMS = (
    re.compile(r"(\w+) must be ((?:\d+/)+\d+)"),
    re.compile(r"(\w+) must be (\d+(?:, \d+)*,? or \d+)"),
)

# Guards stating something this cannot turn into a bound. Each is a real
# constraint the tile enforces; it is simply not a range or a value list, so
# the registry cannot be checked against it here.
UNPARSED_GUARDS = {
    ("rolling-moments", "WINDOW"): "open-ended >= 2; the registry declares the same minimum and no maximum",
    ("stride-distributor", "NUM_PARTITIONS"): "positive multiple of IN_WIDTH/128 -- depends on another parameter",
    ("key-mask-dispatcher", "MODE"): "values carry mode names in the message, so the list is not machine-readable",
}


def _guard_bounds(source: str) -> tuple[dict[str, tuple[int, int]], dict[str, tuple[int, ...]]]:
    """(ranges, choices) keyed by parameter name, read from `$error` text."""
    messages = re.findall(r"\$error\(\"([^\"]+)\"\)", source)
    ranges: dict[str, tuple[int, int]] = {}
    choices: dict[str, tuple[int, ...]] = {}
    for message in messages:
        for pattern in _RANGE_FORMS:
            match = pattern.search(message)
            if match:
                ranges[match.group(1)] = (int(match.group(2)), int(match.group(3)))
                break
        else:
            for pattern in _CHOICE_FORMS:
                match = pattern.search(message)
                if match:
                    choices[match.group(1)] = tuple(int(value) for value in re.findall(r"\d+", match.group(2)))
                    break
    return ranges, choices


def _parameterized_cores() -> list[tuple[str, object]]:
    return [(name, definition) for name, definition in loaded_cores().items() if getattr(definition, "parameters", None)]


def _magnitude(parameter, value: int) -> int:
    """What the tile's guard is a bound ON.

    An element-type parameter carries a type encoding, and a guard reading
    "must divide 128 and lie in [8, 64]" bounds the type's WIDTH -- the
    encoding itself is not a quantity anything is measured in. For a plain
    signed integer the two coincide (int32 encodes as 32), which is exactly
    why this has to be explicit: it would otherwise appear to work until the
    first fixed-point or float type was declared, and then compare 4128
    against a bound about bits.
    """
    return decode_element_type(value).width if parameter.element_type else value


def test_declared_bounds_match_the_tiles_elaboration_guard() -> None:
    checked: list[str] = []
    for name, definition in _parameterized_cores():
        source = (_HDL_DIR / definition.source).read_text(encoding="utf-8")
        ranges, choices = _guard_bounds(source)

        for parameter_name, parameter in definition.parameters.items():
            where = f"{name}.{parameter_name}"

            if parameter_name in ranges:
                low, high = ranges[parameter_name]
                default = _magnitude(parameter, parameter.default)
                if parameter.minimum is not None:
                    assert parameter.minimum == low, f"{where}: registry minimum {parameter.minimum} != elaboration guard {low}"
                if parameter.maximum is not None:
                    assert parameter.maximum == high, f"{where}: registry maximum {parameter.maximum} != elaboration guard {high}"
                assert low <= default <= high, f"{where}: default {default} outside the tile's own [{low}, {high}]"
                for choice in parameter.choices or ():
                    magnitude = _magnitude(parameter, choice)
                    assert low <= magnitude <= high, f"{where}: declared choice {magnitude} outside the tile's own [{low}, {high}]"
                checked.append(where)

            elif parameter_name in choices:
                legal = choices[parameter_name]
                declared = tuple(_magnitude(parameter, choice) for choice in parameter.choices or ())
                if parameter.choices:
                    assert tuple(sorted(declared)) == tuple(sorted(legal)), (
                        f"{where}: registry offers {sorted(declared)} but the tile elaborates only {sorted(legal)}"
                    )
                default = _magnitude(parameter, parameter.default)
                assert default in legal, f"{where}: default {default} is not one the tile elaborates {legal}"
                checked.append(where)

    assert len(checked) >= 8, f"parsed almost no bounds -- the guard wording has changed: {checked}"


def test_unparsed_guards_are_declared_and_still_exist() -> None:
    """The holdout list must not outlive its holdouts, in either direction."""
    cores = dict(_parameterized_cores())
    for (core_name, parameter_name), reason in UNPARSED_GUARDS.items():
        assert core_name in cores, f"{core_name} no longer has registry parameters -- drop it from UNPARSED_GUARDS ({reason})"
        assert parameter_name in cores[core_name].parameters, f"{core_name}.{parameter_name} is gone -- drop it from UNPARSED_GUARDS"

        source = (_HDL_DIR / cores[core_name].source).read_text(encoding="utf-8")
        ranges, choices = _guard_bounds(source)
        assert parameter_name not in ranges and parameter_name not in choices, (
            f"{core_name}.{parameter_name} is parseable now -- remove it from UNPARSED_GUARDS so its bounds get checked"
        )


def test_the_python_mirror_of_the_top_k_bound_matches_the_registry() -> None:
    """`TOP_K_MAX` is a third copy of the same number: the compiler rejects an
    out-of-envelope limit against it, the registry prices against its maximum,
    and the tile refuses to elaborate outside its guard."""
    from dau_core.operators import TOP_K_MAX

    parameter = loaded_cores()["streaming-top-k"].parameters["K"]
    assert TOP_K_MAX == parameter.maximum, f"TOP_K_MAX {TOP_K_MAX} != registry maximum {parameter.maximum}"

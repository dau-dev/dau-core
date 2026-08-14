"""The element type vocabulary, and the two places it is written down.

An encoding stated in Python and again in SystemVerilog is an encoding that
can disagree with itself -- the same failure mode ``test_parameter_bounds
_match_the_hdl`` exists for. Verilog cannot read ``element_types.py`` and the
registry cannot read ``types.svh``, so both are necessary and something has to
hold them to the same numbers. That is what the last test here does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from dau_core import hdl
from dau_core.element_types import (
    ElementType,
    ElementTypeError,
    decode_element_type,
    element_code,
    element_type,
    parse_element_type,
)

_TYPES_SVH = Path(str(hdl.DAU_TYPES_SVH))


def test_a_plain_signed_integer_encodes_as_its_own_width() -> None:
    """The hinge the whole migration rests on.

    Every measured envelope, staged design and ``-generic FIELD_WIDTH=32``
    predates element types. They keep their meaning only because int32 IS 32,
    so converting a parameter from a width to a type changes no synthesized
    bit. If this ever stops holding, every coordinate in the registry silently
    names a different design.
    """
    for width in (8, 16, 32, 64):
        assert element_code(f"int{width}") == width
        assert element_code(width) == width


def test_the_three_type_classes_round_trip_through_their_spellings() -> None:
    for spelling in ("int8", "int16", "int32", "int64", "uint32", "float32", "float64", "q16.16", "q1.7", "q24.8"):
        element = parse_element_type(spelling)
        assert element.name == spelling
        assert decode_element_type(element.code) == element


def test_a_type_carries_what_a_width_cannot() -> None:
    """int32, q16.16 and float32 are all 32 bits and all different."""
    integer, fixed, floating = parse_element_type("int32"), parse_element_type("q16.16"), parse_element_type("float32")

    assert integer.width == fixed.width == floating.width == 32
    assert len({integer.code, fixed.code, floating.code}) == 3

    assert (integer.fractional_bits, integer.is_float, integer.signed) == (0, False, True)
    assert (fixed.fractional_bits, fixed.is_float, fixed.signed) == (16, False, True)
    assert (floating.fractional_bits, floating.is_float, floating.signed) == (0, True, True)
    assert not parse_element_type("uint32").signed


def test_q_notation_counts_the_sign_bit_in_the_integer_part() -> None:
    """Q notation is ambiguous in the wild, so the convention is pinned: qI.F
    occupies I+F bits. q16.16 is the 32-bit format the fixed-point tiles
    compute in, which they spell today as FRACTIONAL_BITS=16."""
    fixed = parse_element_type("q16.16")
    assert (fixed.width, fixed.fractional_bits) == (32, 16)
    assert parse_element_type("q8.8").width == 16


def test_an_integer_is_read_as_an_encoding_never_re_interpreted() -> None:
    """One interpretation of an integer, and it is the RTL's.

    A Python side that read 4128 as "4128 bits wide" while the tile it
    configures read it as q16.16 would be precisely the confusion this module
    exists to end.
    """
    assert element_type(element_code("q16.16")) == parse_element_type("q16.16")
    assert element_type(element_code("float64")) == parse_element_type("float64")
    assert element_type(16) == parse_element_type("int16")


def test_spellings_that_name_no_type_are_refused() -> None:
    for spelling in ("int", "i32", "32", "float16", "q16", "q0.32", "int0", "int256", ""):
        with pytest.raises(ElementTypeError):
            parse_element_type(spelling)


def test_float_widths_are_the_ieee_interchange_formats() -> None:
    """ "float24" would name a format with no defined exponent split; refusing
    it is better than inventing one a tile would then have to guess at."""
    with pytest.raises(ElementTypeError, match="interchange format"):
        parse_element_type("float24")
    with pytest.raises(ElementTypeError, match="interchange format"):
        decode_element_type(ElementType(width=16, is_float=True).code)


def test_a_fraction_that_fills_the_field_leaves_no_integer_part() -> None:
    with pytest.raises(ElementTypeError, match="no integer part"):
        parse_element_type("q0.32")


def test_a_code_that_names_nothing_coherent_is_refused() -> None:
    with pytest.raises(ElementTypeError):
        decode_element_type(0)  # width zero
    with pytest.raises(ElementTypeError):
        decode_element_type(-1)
    with pytest.raises(ElementTypeError, match="type spelling or an encoded int"):
        decode_element_type(True)


_DEFINE = re.compile(r"^`define (\w+)(\([^)]*\))?[ \t]+(.*)$", re.MULTILINE)
_INVOCATION = re.compile(r"`(\w+)(?:\(([^()]*)\))?")
_SIZED_LITERAL = re.compile(r"\d+'([hd])")


def _header_macros() -> dict[str, tuple[tuple[str, ...], str]]:
    """Every macro in types.svh, as (parameter names, body)."""
    macros: dict[str, tuple[tuple[str, ...], str]] = {}
    for name, parameters, body in _DEFINE.findall(_TYPES_SVH.read_text(encoding="utf-8")):
        names = tuple(part.strip() for part in parameters[1:-1].split(",")) if parameters else ()
        macros[name] = (names, body.strip())
    return macros


def _evaluate(expression: str) -> int:
    """The value the preprocessor and then the elaborator would arrive at.

    The macros are deliberately plain integer arithmetic, which is what makes
    it possible to check them from here at all -- expand them the way the
    preprocessor does, translate Verilog's sized literals, and evaluate. Doing
    it any other way (recomputing the intended value in Python) would compare
    this module against itself and pass through any drift, which is the one
    thing this must not do.
    """
    macros = _header_macros()

    def substitute(match: re.Match[str]) -> str:
        name, arguments = match.group(1), match.group(2)
        if name not in macros:
            return match.group(0)
        parameters, body = macros[name]
        if not parameters:
            return f"({body})"
        values = [value.strip() for value in (arguments or "").split(",")]
        assert len(values) == len(parameters), f"`{name} takes {parameters}, called with {values}"
        for parameter, value in zip(parameters, values, strict=True):
            body = re.sub(rf"\b{re.escape(parameter)}\b", f"({value})", body)
        return f"({body})"

    for _ in range(8):
        expanded = _INVOCATION.sub(substitute, expression)
        if expanded == expression:
            break
        expression = expanded
    else:
        raise AssertionError(f"macro expansion did not settle: {expression}")

    assert "`" not in expression, f"unexpanded macro in {expression}"
    expression = _SIZED_LITERAL.sub(lambda match: "0x" if match.group(1) == "h" else "", expression)
    return int(eval(expression, {"__builtins__": {}}, {}))


def test_the_header_and_this_module_encode_the_same_types() -> None:
    """The one test that keeps two statements of an encoding honest."""
    macros = _header_macros()
    named = {
        "DAU_TYPE_INT8": "int8",
        "DAU_TYPE_INT16": "int16",
        "DAU_TYPE_INT32": "int32",
        "DAU_TYPE_INT64": "int64",
        "DAU_TYPE_FLOAT32": "float32",
        "DAU_TYPE_FLOAT64": "float64",
        "DAU_TYPE_Q16_16": "q16.16",
    }
    assert set(named) <= set(macros), f"types.svh no longer defines {sorted(set(named) - set(macros))}"
    for macro, spelling in named.items():
        assert _evaluate(f"`{macro}") == element_code(spelling), f"`{macro} and {spelling!r} encode differently"

    # and the constructors agree at types the palette does not name outright
    assert _evaluate("`DAU_TYPE_INT(24)") == element_code("int24")
    assert _evaluate("`DAU_TYPE_UINT(8)") == element_code("uint8")
    assert _evaluate("`DAU_TYPE_Q(24, 8)") == element_code("q24.8")
    assert _evaluate("`DAU_TYPE_FLOAT(64)") == element_code("float64")


def test_the_header_accessors_extract_what_python_stores() -> None:
    """The accessor macros are bit arithmetic; run that arithmetic against
    every spelling and check it recovers the four facts a tile decodes."""
    accessors = {
        "DAU_TYPE_WIDTH": lambda element: element.width,
        "DAU_TYPE_FRAC_BITS": lambda element: element.fractional_bits,
        "DAU_TYPE_IS_FLOAT": lambda element: int(element.is_float),
        "DAU_TYPE_IS_SIGNED": lambda element: int(element.signed),
    }
    macros = _header_macros()
    assert set(accessors) <= set(macros), f"types.svh no longer defines {sorted(set(accessors) - set(macros))}"

    for spelling in ("int8", "int64", "uint32", "float32", "float64", "q16.16", "q24.8"):
        element = parse_element_type(spelling)
        for macro, expected in accessors.items():
            assert _evaluate(f"`{macro}({element.code})") == expected(element), f"`{macro} disagrees with {spelling!r}"

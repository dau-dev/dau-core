"""Element types: what a value IS, not how wide it is.

A registry parameter that says ``32`` says a field is 32 bits. It does not say
whether those bits are a signed integer, a Q16.16 fixed-point number or an
IEEE-754 binary32 -- three things a tile must treat differently and which a
width cannot distinguish. This module gives the palette's element types names
("int32", "q16.16", "float64") and turns them into the single integer a
SystemVerilog parameter can carry.

THE ENCODING is stated in ``dau_core/hdl/types.svh`` and mirrored here,
because Verilog cannot read Python and the registry cannot read a header.
``test_element_types.py`` holds the two to the same numbers.

    [7:0]   total width in bits (1..255)
    [15:8]  fractional bits (0 for integers and for floats)
    [16]    1 = IEEE-754 binary floating point
    [17]    1 = unsigned

A PLAIN SIGNED INTEGER ENCODES AS ITS OWN WIDTH, so ``int32`` is 32 -- the
number the palette already carried. Migrating a parameter from a width to a
type therefore changes no synthesized bit, no measured envelope, and no
staged design; only the types a width could never express encode to anything
new. It is also why a bare integer stays a legal spelling: a caller who means
"a signed integer this many bits wide", or who wants a width the palette has
no name for, writes the number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

WIDTH_MASK = 0xFF
FRACTIONAL_SHIFT = 8
FRACTIONAL_MASK = 0xFF
FLOAT_BIT = 1 << 16
UNSIGNED_BIT = 1 << 17

MAX_WIDTH = WIDTH_MASK

# IEEE-754 has exactly these two binary interchange formats in the palette;
# "float24" would name a format with no defined exponent/mantissa split, so
# the parser refuses it rather than inventing one.
FLOAT_WIDTHS = (32, 64)

_INTEGER = re.compile(r"\A(u?)int(\d+)\Z")
_FLOAT = re.compile(r"\Afloat(\d+)\Z")
_FIXED_POINT = re.compile(r"\Aq(\d+)\.(\d+)\Z")


class ElementTypeError(ValueError):
    """A spelling that names no element type."""


@dataclass(frozen=True)
class ElementType:
    """One element type: the four facts a tile needs, plus its encoding.

    Constructed by :func:`element_type` rather than directly, so that every
    instance has been through the same validity rules.
    """

    width: int
    fractional_bits: int = 0
    signed: bool = True
    is_float: bool = False

    @property
    def code(self) -> int:
        """The integer a SystemVerilog parameter carries for this type."""
        code = self.width | (self.fractional_bits << FRACTIONAL_SHIFT)
        if self.is_float:
            code |= FLOAT_BIT
        if not self.signed:
            code |= UNSIGNED_BIT
        return code

    @property
    def name(self) -> str:
        """The canonical spelling, which parses back to this same type."""
        if self.is_float:
            return f"float{self.width}"
        if self.fractional_bits:
            return f"q{self.width - self.fractional_bits}.{self.fractional_bits}"
        return f"{'' if self.signed else 'u'}int{self.width}"

    def __str__(self) -> str:
        return self.name


def _checked(element: ElementType, source: object) -> ElementType:
    if not 1 <= element.width <= MAX_WIDTH:
        raise ElementTypeError(f"element type {source!r} has width {element.width}, outside [1, {MAX_WIDTH}]")
    if element.fractional_bits >= element.width:
        # the sign bit alone leaves qI.F needing I >= 1; an all-fraction format
        # has no integer part at all and no tile can address its high field
        raise ElementTypeError(f"element type {source!r} spends all {element.width} bits on the fraction, leaving no integer part")
    if element.is_float and element.width not in FLOAT_WIDTHS:
        raise ElementTypeError(f"element type {source!r} is not an IEEE-754 interchange format; float widths are {FLOAT_WIDTHS}")
    return element


def parse_element_type(spelling: str) -> ElementType:
    """The element type a registry/composition spelling names.

    ``int8``/``int16``/``int32``/``int64`` and their ``uint`` counterparts,
    ``float32``/``float64``, and the general fixed-point form ``qI.F`` (I
    integer bits including the sign, F fractional bits, I+F total).
    """
    text = spelling.strip()

    match = _INTEGER.match(text)
    if match:
        return _checked(ElementType(width=int(match.group(2)), signed=not match.group(1)), spelling)

    match = _FLOAT.match(text)
    if match:
        return _checked(ElementType(width=int(match.group(1)), is_float=True), spelling)

    match = _FIXED_POINT.match(text)
    if match:
        integer_bits, fractional_bits = int(match.group(1)), int(match.group(2))
        return _checked(ElementType(width=integer_bits + fractional_bits, fractional_bits=fractional_bits), spelling)

    raise ElementTypeError(
        f"{spelling!r} names no element type; write intN/uintN (int32), floatN (float64), qI.F (q16.16), or a plain integer bit width"
    )


def decode_element_type(code: int) -> ElementType:
    """The element type an encoded parameter value carries."""
    if isinstance(code, bool) or not isinstance(code, int):
        raise ElementTypeError(f"element type {code!r} must be a type spelling or an encoded int")
    if code < 0:
        raise ElementTypeError(f"element type code {code!r} is negative")
    return _checked(
        ElementType(
            width=code & WIDTH_MASK,
            fractional_bits=(code >> FRACTIONAL_SHIFT) & FRACTIONAL_MASK,
            signed=not code & UNSIGNED_BIT,
            is_float=bool(code & FLOAT_BIT),
        ),
        code,
    )


def element_type(value: int | str) -> ElementType:
    """The element type a declared value names, in either accepted form.

    A STRING is a type spelling. An INTEGER is the encoding -- which, for a
    plain signed integer, IS its width, so ``16`` still means ``int16`` and
    is the only integer form worth hand-writing. There is deliberately ONE
    interpretation of an integer here, and it is the same one the RTL applies
    to the parameter it receives: a Python side that read ``16`` as a width
    while Verilog read it as a code would be the exact confusion this module
    exists to end.
    """
    if isinstance(value, str):
        return parse_element_type(value)
    return decode_element_type(value)


def element_code(value: int | str) -> int:
    """The encoded parameter value for a declared element type."""
    return element_type(value).code

"""Byte layouts of the rows and records the grouped and bar tiles exchange
with the host.

A layout is a tuple of ``(field, kind)`` pairs in wire order, little-endian,
with ``kind`` a numpy-style code (``i4``, ``u4``, ``i8``, ``u8``). The tiles'
registry entries declare the same records in 64-bit words (``record_words``
and ``output_record_words``), and the suite checks the two agree, so a tile
that changes its record width fails there rather than on the host.

Field names follow the goldens' result types (``GroupAggregate``,
``BarAggregate``); ``pad`` fields hold no data and keep the wider fields on
their natural alignment.
"""

from __future__ import annotations

import struct

__all__ = (
    "BAR_RECORD_BYTES",
    "BAR_RECORD_FIELDS",
    "BAR_ROW_BYTES",
    "BAR_ROW_FIELDS",
    "GROUP_RECORD_BYTES",
    "GROUP_RECORD_FIELDS",
    "KEYED_ROW_BYTES",
    "KEYED_ROW_FIELDS",
    "numpy_dtype_fields",
    "record_bytes",
    "struct_format",
)

Layout = tuple[tuple[str, str], ...]

_STRUCT_CODES = {"i4": "i", "u4": "I", "i8": "q", "u8": "Q"}

# grouped-aggregation input: one (key, value) pair per row
KEYED_ROW_FIELDS: Layout = (("key", "i4"), ("value", "u4"))
# grouped-aggregation output: one closed group
GROUP_RECORD_FIELDS: Layout = (
    ("key", "i4"),
    ("first", "i4"),
    ("last", "i4"),
    ("minimum", "i4"),
    ("maximum", "i4"),
    ("pad", "i4"),
    ("total", "i8"),
    ("count", "u8"),
)
# bar-aggregation input: one trade
BAR_ROW_FIELDS: Layout = (("key", "i4"), ("price", "i4"), ("shares", "i4"), ("frac", "i4"))
# bar-aggregation output: one closed bar
BAR_RECORD_FIELDS: Layout = (
    ("key", "i4"),
    ("open", "i4"),
    ("close", "i4"),
    ("low", "i4"),
    ("high", "i4"),
    ("pad", "i4"),
    ("shares", "i8"),
    ("frac", "i8"),
    ("count", "u8"),
)


def struct_format(fields: Layout) -> str:
    """The little-endian ``struct`` format for a layout."""
    return "<" + "".join(_STRUCT_CODES[kind] for _name, kind in fields)


def numpy_dtype_fields(fields: Layout) -> list[tuple[str, str]]:
    """The same layout as numpy dtype pairs, for vectorized consumers."""
    return [(name, "<" + kind) for name, kind in fields]


def record_bytes(fields: Layout) -> int:
    """Bytes per row or record of a layout."""
    return struct.calcsize(struct_format(fields))


KEYED_ROW_BYTES = record_bytes(KEYED_ROW_FIELDS)
GROUP_RECORD_BYTES = record_bytes(GROUP_RECORD_FIELDS)
BAR_ROW_BYTES = record_bytes(BAR_ROW_FIELDS)
BAR_RECORD_BYTES = record_bytes(BAR_RECORD_FIELDS)

"""The host's record layouts agree with what the registry says the tiles read
and write, and with the goldens' result types."""

from __future__ import annotations

import dataclasses
import struct

import pytest

from dau_core.operators import BarAggregate, GroupAggregate
from dau_core.records import (
    BAR_RECORD_BYTES,
    BAR_RECORD_FIELDS,
    BAR_ROW_BYTES,
    BAR_ROW_FIELDS,
    GROUP_RECORD_BYTES,
    GROUP_RECORD_FIELDS,
    KEYED_ROW_BYTES,
    KEYED_ROW_FIELDS,
    numpy_dtype_fields,
    struct_format,
)
from dau_core.registry import core

WORD_BYTES = 8


@pytest.mark.parametrize(
    ("tile", "row_bytes", "record_bytes"),
    [("grouped-aggregation", KEYED_ROW_BYTES, GROUP_RECORD_BYTES), ("bar-aggregation", BAR_ROW_BYTES, BAR_RECORD_BYTES)],
)
def test_each_layout_is_as_wide_as_the_registry_says(tile: str, row_bytes: int, record_bytes: int) -> None:
    definition = core(tile)
    assert definition.record_words is not None and definition.output_record_words is not None
    assert row_bytes == definition.record_words * WORD_BYTES
    assert record_bytes == definition.output_record_words * WORD_BYTES


@pytest.mark.parametrize(("fields", "result_type"), [(GROUP_RECORD_FIELDS, GroupAggregate), (BAR_RECORD_FIELDS, BarAggregate)])
def test_each_record_carries_the_goldens_fields_in_order(fields, result_type) -> None:
    assert [name for name, _kind in fields if name != "pad"] == [field.name for field in dataclasses.fields(result_type)]


def test_the_struct_and_numpy_views_agree() -> None:
    numpy = pytest.importorskip("numpy")
    for fields in (KEYED_ROW_FIELDS, GROUP_RECORD_FIELDS, BAR_ROW_FIELDS, BAR_RECORD_FIELDS):
        assert numpy.dtype(numpy_dtype_fields(fields)).itemsize == struct.calcsize(struct_format(fields))
    assert struct_format(BAR_RECORD_FIELDS) == "<iiiiiiqqQ"

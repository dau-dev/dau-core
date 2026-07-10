from __future__ import annotations

from pathlib import Path

import pytest

from dau_core.design import DauDesign, OperatorUnit, UnitKind, format_units, parse_units
from dau_core.stream import LogicalType, OperationCode

_AGG_UNIT = OperatorUnit(
    kind=UnitKind.AGGREGATION,
    count=2,
    opcodes=(OperationCode.SUM, OperationCode.MIN, OperationCode.MAX, OperationCode.COUNT),
    input_types=(LogicalType.INT32,),
)


def test_operator_unit_rejects_negative_count() -> None:
    with pytest.raises(ValueError, match="count cannot be negative"):
        OperatorUnit(kind=UnitKind.JOIN, count=-1)


def test_aggregation_capacity_counts_matching_units_only() -> None:
    design = DauDesign(name="d", units=(_AGG_UNIT, OperatorUnit(kind=UnitKind.JOIN, count=1)))

    assert design.aggregation_capacity(OperationCode.SUM, LogicalType.INT32) == 2
    assert design.aggregation_capacity(OperationCode.AVERAGE, LogicalType.INT32) == 0
    assert design.aggregation_capacity(OperationCode.SUM, LogicalType.INT64) == 0
    assert design.capacity(UnitKind.JOIN) == 1
    assert design.capacity(UnitKind.SORT) == 0


def test_units_grammar_round_trips() -> None:
    units = (_AGG_UNIT, OperatorUnit(kind=UnitKind.JOIN, count=1))
    encoded = format_units(units)
    assert encoded == "aggregation:2:sum|min|max|count:int32,join:1::"
    assert parse_units(encoded) == units


def test_parse_units_empty_is_empty_tuple() -> None:
    assert parse_units("") == ()
    assert parse_units("   ") == ()


def test_parse_units_rejects_malformed_entry() -> None:
    with pytest.raises(ValueError, match="malformed unit entry"):
        parse_units("aggregation:1")


def test_from_manifest_reads_units_line(tmp_path: Path) -> None:
    manifest = tmp_path / "design.manifest"
    manifest.write_text(
        "\n".join(
            (
                "builder=dau_build.build_spec",
                "design_name=arrow-lite",
                "units=aggregation:1:sum|min|max|count:int32",
                "",
            )
        )
    )

    design = DauDesign.from_manifest(manifest)

    assert design.name == "arrow-lite"
    assert design.aggregation_capacity(OperationCode.COUNT, LogicalType.INT32) == 1
    assert design.capacity(UnitKind.JOIN) == 0

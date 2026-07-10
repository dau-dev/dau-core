from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .stream import LogicalType, OperationCode


class UnitKind(StrEnum):
    AGGREGATION = "aggregation"
    JOIN = "join"
    SORT = "sort"


@dataclass(frozen=True)
class OperatorUnit:
    """A count of identical hardware operator engines and what they accept.

    ``opcodes`` and ``input_types`` describe aggregation coverage; join and sort
    units currently carry no opcode/type detail and exist as capacity only.
    """

    kind: UnitKind
    count: int
    opcodes: tuple[OperationCode, ...] = ()
    input_types: tuple[LogicalType, ...] = ()

    def __post_init__(self) -> None:
        if self.count < 0:
            raise ValueError("unit count cannot be negative")

    def supports(self, opcode: OperationCode, logical_type: LogicalType) -> bool:
        return opcode in self.opcodes and logical_type in self.input_types


@dataclass(frozen=True)
class DauDesign:
    """The operator inventory a synthesized DAU design advertises."""

    name: str
    units: tuple[OperatorUnit, ...] = ()

    def units_of(self, kind: UnitKind) -> tuple[OperatorUnit, ...]:
        return tuple(unit for unit in self.units if unit.kind == kind)

    def capacity(self, kind: UnitKind) -> int:
        return sum(unit.count for unit in self.units_of(kind))

    def aggregation_capacity(self, opcode: OperationCode, logical_type: LogicalType) -> int:
        return sum(unit.count for unit in self.units_of(UnitKind.AGGREGATION) if unit.supports(opcode, logical_type))

    def manifest_items(self) -> tuple[tuple[str, str], ...]:
        return (
            ("design_name", self.name),
            ("units", format_units(self.units)),
        )

    @classmethod
    def from_manifest_mapping(cls, mapping: dict[str, str]) -> DauDesign:
        return cls(name=mapping.get("design_name", ""), units=parse_units(mapping.get("units", "")))

    @classmethod
    def from_manifest(cls, path: str | Path) -> DauDesign:
        mapping: dict[str, str] = {}
        for line in Path(path).read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            mapping[key.strip()] = value.strip()
        return cls.from_manifest_mapping(mapping)


def format_units(units: tuple[OperatorUnit, ...]) -> str:
    return ",".join(
        ":".join(
            (
                unit.kind.value,
                str(unit.count),
                "|".join(opcode.name.lower() for opcode in unit.opcodes),
                "|".join(logical_type.name.lower() for logical_type in unit.input_types),
            )
        )
        for unit in units
    )


def parse_units(value: str) -> tuple[OperatorUnit, ...]:
    if not value.strip():
        return ()
    units: list[OperatorUnit] = []
    for entry in value.split(","):
        fields = entry.split(":")
        if len(fields) != 4:
            raise ValueError(f"malformed unit entry {entry!r}; expected kind:count:opcodes:types")
        kind, count, opcodes, input_types = fields
        units.append(
            OperatorUnit(
                kind=UnitKind(kind),
                count=int(count),
                opcodes=tuple(OperationCode[token.upper()] for token in opcodes.split("|") if token),
                input_types=tuple(LogicalType[token.upper()] for token in input_types.split("|") if token),
            )
        )
    return tuple(units)

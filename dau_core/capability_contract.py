"""Composition-derived capability contract: the single source of truth for
what a generated shell can do.

``capability_contract(spec)`` derives the contract from the same
``ScanCompositionSpec`` that generates the shell top, using the registry's
declared HDL facts (opcodes, input types, count ports, contract kinds, row
capacities). Three consumers read it:

- the identity register block (``operator_bitmap`` / ``lane_count`` /
  ``host_opcode_bitmap`` / ``sort_capacity`` words, register map 0.2);
- the shell-build artlink manifest (``to_mapping()``, JSON-able);
- host planners (``to_design()`` for the pushdown scheduler,
  driver capability decode via the manifest mapping).

Hardware truth vs protocol truth is explicit: ``operator_bitmap`` carries
only OperationCodes some tile executes in HDL (MIN/MAX/SUM/COUNT);
AVERAGE is never a hardware opcode — when a tile provides both SUM and
COUNT it appears in ``host_opcodes`` (the host divides the results at
protocol level), never in ``opcodes``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from .composition import ScanCompositionSpec, TileSpec
from .cores import ContractKind
from .design import DauDesign, OperatorUnit, UnitKind
from .models import CheckedModel
from .registers import CURRENT_REGISTER_MAP_VERSION, RegisterMapVersion
from .registry import core
from .stream import LogicalType, OperationCode

CAPABILITY_CONTRACT_SCHEMA = 1


def opcode_bitmap(opcodes: tuple[OperationCode, ...]) -> int:
    """The register encoding: bit ``1 << opcode`` per OperationCode."""
    bitmap = 0
    for opcode in opcodes:
        bitmap |= 1 << int(opcode)
    return bitmap


class TileCapability(CheckedModel):
    """One operator tile instance as the contract describes it: registry
    identity, stream contract, HDL opcode truth, instance config, and the
    module parameters it was built with (each declared parameter, at its
    override or its default)."""

    core: str
    module: str
    contract_kind: ContractKind | None
    count_port: str | None
    opcodes: tuple[OperationCode, ...] = ()
    input_types: tuple[LogicalType, ...] = ()
    unit_kind: UnitKind | None = None
    row_capacity: int | None = None
    config: tuple[tuple[str, str], ...] = ()
    params: tuple[tuple[str, int], ...] = ()

    @property
    def host_opcodes(self) -> tuple[OperationCode, ...]:
        """Protocol-level opcodes the host can compute from this tile's
        hardware results: AVERAGE = SUM/COUNT divided host-side. Never
        hardware opcodes and never advertised as such."""
        if OperationCode.SUM in self.opcodes and OperationCode.COUNT in self.opcodes:
            return (OperationCode.AVERAGE,)
        return ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "core": self.core,
            "module": self.module,
            "contract_kind": self.contract_kind.value if self.contract_kind is not None else None,
            "count_port": self.count_port,
            "opcodes": [opcode.name.lower() for opcode in self.opcodes],
            "input_types": [logical_type.name.lower() for logical_type in self.input_types],
            "unit_kind": self.unit_kind.value if self.unit_kind is not None else None,
            "row_capacity": self.row_capacity,
            "config": dict(self.config),
            "params": dict(self.params),
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> TileCapability:
        return cls(
            core=mapping["core"],
            module=mapping["module"],
            contract_kind=ContractKind(mapping["contract_kind"]) if mapping.get("contract_kind") else None,
            count_port=mapping.get("count_port"),
            opcodes=tuple(OperationCode[token.upper()] for token in mapping.get("opcodes", ())),
            input_types=tuple(LogicalType[token.upper()] for token in mapping.get("input_types", ())),
            unit_kind=UnitKind(mapping["unit_kind"]) if mapping.get("unit_kind") else None,
            row_capacity=mapping.get("row_capacity"),
            config=tuple(sorted((str(key), str(value)) for key, value in dict(mapping.get("config", {})).items())),
            # absent in manifests written before the fact was advertised; a
            # reader that needs a parameter treats it as unknown
            params=tuple(sorted((str(key), int(value)) for key, value in dict(mapping.get("params", {})).items())),
        )


class LaneCapability(CheckedModel):
    """One scan lane: the terminal operator tile, its optional partition
    filter, and the ordered mid-lane operator ``chain`` between them."""

    tile: TileCapability
    partition: TileCapability | None = None
    chain: tuple[TileCapability, ...] = ()

    def to_mapping(self) -> dict[str, Any]:
        return {
            "tile": self.tile.to_mapping(),
            "partition": self.partition.to_mapping() if self.partition is not None else None,
            "chain": [stage.to_mapping() for stage in self.chain],
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> LaneCapability:
        partition = mapping.get("partition")
        return cls(
            tile=TileCapability.from_mapping(mapping["tile"]),
            partition=TileCapability.from_mapping(partition) if partition else None,
            chain=tuple(TileCapability.from_mapping(stage) for stage in mapping.get("chain", ())),
        )


class CapabilityContract(CheckedModel):
    """The advertised capability truth of one composed design.

    A config-data model (it is what the shell-build manifest carries and
    what a planner matches against), so a ccflow model like the rest of the
    registry's declarations; ``to_mapping``/``from_mapping`` remain the
    manifest serialization, unchanged, because manifests already on disk
    carry that form."""

    name: str
    lanes: tuple[LaneCapability, ...]
    partitioner: TileCapability | None = None
    register_map_version: RegisterMapVersion = Field(default_factory=lambda: CURRENT_REGISTER_MAP_VERSION)
    # whether the lanes' chain config ports are runtime registers the host
    # rewrites per job, so a planner matches this design structurally
    # (enough stages of the right kinds) instead of config for config. A
    # fact about the bitstream, carried in its manifest; never a caller's
    # declaration.
    reconfigurable: bool = False

    @property
    def lane_count(self) -> int:
        return len(self.lanes)

    @property
    def hardware_opcodes(self) -> tuple[OperationCode, ...]:
        """Union of the lanes' HDL opcodes (terminal tiles and chain
        stages), ordered by opcode value."""
        opcodes = {opcode for lane in self.lanes for tile in (lane.tile, *lane.chain) for opcode in tile.opcodes}
        return tuple(sorted(opcodes, key=int))

    @property
    def host_opcodes(self) -> tuple[OperationCode, ...]:
        """Protocol-level opcodes derivable host-side (AVERAGE when a tile
        provides both SUM and COUNT); explicitly not hardware opcodes."""
        opcodes = {opcode for lane in self.lanes for tile in (lane.tile, *lane.chain) for opcode in tile.host_opcodes}
        return tuple(sorted(opcodes, key=int))

    @property
    def operator_bitmap(self) -> int:
        return opcode_bitmap(self.hardware_opcodes)

    @property
    def host_opcode_bitmap(self) -> int:
        return opcode_bitmap(self.host_opcodes)

    @property
    def sort_capacity(self) -> int:
        capacities = [lane.tile.row_capacity or 0 for lane in self.lanes if lane.tile.unit_kind == UnitKind.SORT]
        return max(capacities, default=0)

    def to_design(self) -> DauDesign:
        """The operator inventory this contract advertises: identical lane
        tiles that contribute a design unit collapse into one counted unit."""
        counts: dict[TileCapability, int] = {}
        for lane in self.lanes:
            if lane.tile.unit_kind is None:
                continue
            key = TileCapability(
                core=lane.tile.core,
                module=lane.tile.module,
                contract_kind=lane.tile.contract_kind,
                count_port=lane.tile.count_port,
                opcodes=lane.tile.opcodes,
                input_types=lane.tile.input_types,
                unit_kind=lane.tile.unit_kind,
                row_capacity=lane.tile.row_capacity,
            )
            counts[key] = counts.get(key, 0) + 1
        unit_list: list[OperatorUnit] = []
        for tile, count in counts.items():
            if tile.unit_kind is not None:
                unit_list.append(OperatorUnit(kind=tile.unit_kind, count=count, opcodes=tile.opcodes, input_types=tile.input_types))
        units = tuple(unit_list)
        return DauDesign(name=self.name, units=units)

    def to_mapping(self) -> dict[str, Any]:
        """JSON-able serialization for the shell-build manifest."""
        return {
            "contract": "dau-capability-contract",
            "schema": CAPABILITY_CONTRACT_SCHEMA,
            "name": self.name,
            "register_map_version": f"{self.register_map_version.major}.{self.register_map_version.minor}",
            "operator_bitmap": f"0x{self.operator_bitmap:08x}",
            "host_opcode_bitmap": f"0x{self.host_opcode_bitmap:08x}",
            "lane_count": self.lane_count,
            "sort_capacity": self.sort_capacity,
            "lanes": [lane.to_mapping() for lane in self.lanes],
            "partitioner": self.partitioner.to_mapping() if self.partitioner is not None else None,
            "reconfigurable": self.reconfigurable,
        }

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> CapabilityContract:
        if mapping.get("contract") != "dau-capability-contract":
            raise ValueError("not a dau capability contract mapping")
        schema = mapping.get("schema")
        if schema != CAPABILITY_CONTRACT_SCHEMA:
            raise ValueError(f"unsupported capability contract schema {schema!r}")
        major, _, minor = str(mapping["register_map_version"]).partition(".")
        partitioner = mapping.get("partitioner")
        return cls(
            name=mapping["name"],
            lanes=tuple(LaneCapability.from_mapping(lane) for lane in mapping.get("lanes", ())),
            partitioner=TileCapability.from_mapping(partitioner) if partitioner else None,
            register_map_version=RegisterMapVersion(int(major), int(minor)),
            # absent in manifests written before the fact was advertised:
            # every one of those designs binds its configs as literals
            reconfigurable=bool(mapping.get("reconfigurable", False)),
        )


def tile_capability(tile: TileSpec | str) -> TileCapability:
    """The registry-declared capability facts of one tile instance."""
    spec = TileSpec(core=tile) if isinstance(tile, str) else tile
    definition = core(spec.core)
    return TileCapability(
        core=definition.name,
        module=definition.module,
        contract_kind=definition.contract_kind,
        count_port=definition.count_port,
        opcodes=definition.opcodes,
        input_types=definition.opcode_input_types,
        unit_kind=definition.unit_kind,
        row_capacity=definition.row_capacity,
        config=tuple(sorted((str(key), str(value)) for key, value in spec.config.items())),
        params=tuple(sorted((name, spec.params.get(name, parameter.default)) for name, parameter in definition.parameters.items())),
    )


def capability_contract(spec: ScanCompositionSpec) -> CapabilityContract:
    """Derive the capability contract from the composition that generates
    the shell top — same spec, same registry, one source of truth.

    ``reconfigurable`` stays false: the generated walker binds every config
    port to the literal the composition names, so no composed design has
    runtime config registers yet. When the generator can bind them to
    registers, the composition will say so and this is where the contract
    reads it."""
    spec = spec.check()
    return CapabilityContract(
        name=spec.name,
        lanes=tuple(
            LaneCapability(
                tile=tile_capability(lane.tile),
                partition=tile_capability(lane.partition) if lane.partition is not None else None,
                chain=tuple(tile_capability(stage) for stage in lane.chain),
            )
            for lane in spec.lanes
        ),
        partitioner=tile_capability(spec.partitioner) if spec.partitioner is not None else None,
    )

"""Machine-readable core registry: every HDL building block, what it is,
what it depends on, which golden defines its semantics, and what it costs.

This is the inventory a shell composer or query planner enumerates: until
now the tile ↔ golden ↔ bench association lived only in naming convention
enforced by tests, and utilization numbers were written to reports and
never read back. Resource envelopes are measured out-of-context or
routed-design attributions; ``measured=False`` marks estimates awaiting a
synthesis pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from importlib import import_module
from pathlib import Path

from .design import UnitKind
from .stream import LogicalType, OperationCode


class CoreKind(Enum):
    OPERATOR = "operator"
    STREAM_INFRA = "stream-infra"
    SHELL_ADAPTER = "shell-adapter"
    PACKAGE = "package"
    SIM_MODEL = "sim-model"


class ContractKind(Enum):
    """Which streaming interface contract an operator tile speaks. Contract
    tests select their validator from this tag; a new core declares its
    contract here rather than being special-cased in test code."""

    ROW_STREAM = "row-stream"  # the standard 64-bit stream + status tile contract
    ARROW_LITE_DESCRIPTOR = "arrow-lite-descriptor"  # descriptor-driven Arrow-lite protocol


@dataclass(frozen=True)
class ResourceEnvelope:
    """Post-synthesis footprint on the target part (dpv1 XC7A200T unless
    noted). ``measured=False`` marks an engineering estimate."""

    lut: int
    ff: int
    bram36: float
    dsp: int
    measured: bool = True


@dataclass(frozen=True)
class CoreDefinition:
    """One HDL building block and its contract surround."""

    name: str
    kind: CoreKind
    module: str
    source: str
    description: str
    depends: tuple[str, ...] = ()
    golden: str | None = None  # "module:callable" defining the semantics
    bench: str | None = None  # cocotb bench module
    resources: ResourceEnvelope | None = None
    count_port: str | None = None  # the tile's trailing status counter, if any
    legacy: bool = False  # predates the stream+status contract; kept verified, not for new compositions
    contract_kind: ContractKind | None = None  # required for non-legacy operators; None elsewhere (no declared stream contract)
    # capability facts (HDL truth, consumed by the composition-derived
    # capability contract): the descriptor-selectable OperationCodes a tile
    # executes in hardware and the logical types those opcodes accept.
    # AVERAGE is never declared here — it is not a hardware opcode (the host
    # divides the SUM/COUNT results at protocol level).
    opcodes: tuple[OperationCode, ...] = ()
    opcode_input_types: tuple[LogicalType, ...] = ()
    row_capacity: int | None = None  # batch row capacity for buffered tiles (bitonic sorter CAPACITY)
    unit_kind: UnitKind | None = None  # the design-inventory unit an instance of this tile contributes
    # HDL status truth: True when the tile emits a status close-out after
    # every successfully completed batch (terminal-tile behavior — the record
    # writer's done waits on it); False when the tile is silent on success
    # and raises status only for what the downstream cannot see (zero-rows
    # compaction, torn row, bad config — the chain-stage/partition contract,
    # exactly one close-out per lane per batch).
    success_close_out: bool = False

    def source_path(self) -> Path:
        from . import hdl

        return Path(str(getattr(hdl, "__path__")[0])) / self.source

    def golden_callable(self):
        if self.golden is None:
            return None
        module_name, _, attr = self.golden.partition(":")
        return getattr(import_module(module_name), attr)


_BENCHES = "dau_core.tests.cocotb_benches"

CORES: tuple[CoreDefinition, ...] = (
    # packages
    CoreDefinition(
        name="aggregation-pkg",
        kind=CoreKind.PACKAGE,
        module="dau_aggregation_pkg",
        source="dau_aggregation_pkg.sv",
        description="Shared accumulate-step definition for every aggregation core.",
    ),
    CoreDefinition(
        name="predicate-pkg",
        kind=CoreKind.PACKAGE,
        module="dau_predicate_pkg",
        source="dau_predicate_pkg.sv",
        description="Predicate opcodes and comparison step.",
    ),
    CoreDefinition(
        name="map-alu-pkg",
        kind=CoreKind.PACKAGE,
        module="dau_map_alu_pkg",
        source="dau_map_alu_pkg.sv",
        description="Map/ALU opcodes and arithmetic step.",
    ),
    # operator tiles (the standard 64-bit stream + status contract)
    CoreDefinition(
        name="int32-predicate-filter",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_predicate_filter",
        source="dau_int32_predicate_filter.sv",
        description="Compacting predicate filter over INT32 values.",
        depends=("predicate-pkg",),
        golden="dau_core.operators:filter_values",
        bench=f"{_BENCHES}.dau_int32_predicate_filter_tb",
        resources=ResourceEnvelope(lut=985, ff=376, bram36=0.0, dsp=0),
        count_port="selected_count",
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-keyed-predicate-filter",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_keyed_predicate_filter",
        source="dau_int32_keyed_predicate_filter.sv",
        description="Row-atomic keyed predicate filter (composed-pipeline stage).",
        depends=("predicate-pkg",),
        golden="dau_core.operators:filter_rows",
        resources=ResourceEnvelope(lut=644, ff=342, bram36=0.0, dsp=0),
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-map-alu",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_map_alu",
        source="dau_int32_map_alu.sv",
        description="Per-value ALU map over INT32 values.",
        depends=("map-alu-pkg",),
        golden="dau_core.operators:map_values",
        bench=f"{_BENCHES}.dau_int32_map_alu_tb",
        resources=ResourceEnvelope(lut=1338, ff=383, bram36=0.0, dsp=8),
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-keyed-map-alu",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_keyed_map_alu",
        source="dau_int32_keyed_map_alu.sv",
        description="Keyed lane-selectable ALU map (composed-pipeline stage).",
        depends=("map-alu-pkg",),
        golden="dau_core.operators:map_rows",
        resources=ResourceEnvelope(lut=904, ff=316, bram36=0.0, dsp=4),
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-grouped-aggregation",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_grouped_aggregation",
        source="dau_int32_grouped_aggregation.sv",
        description="Ordered close-out grouped aggregation (first/last/min/max/sum/count).",
        golden="dau_core.operators:grouped_aggregate_rows",
        bench=f"{_BENCHES}.dau_int32_grouped_aggregation_tb",
        resources=ResourceEnvelope(lut=672, ff=563, bram36=0.0, dsp=0),
        count_port="group_count",
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-bar-aggregation",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_bar_aggregation",
        source="dau_int32_bar_aggregation.sv",
        description="Fused single-pass OHLCV bar aggregation over 16-byte rows.",
        golden="dau_core.operators:bar_aggregate_rows",
        bench=f"{_BENCHES}.dau_int32_bar_aggregation_tb",
        resources=ResourceEnvelope(lut=829, ff=631, bram36=0.0, dsp=0),
        count_port="bar_count",
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-pair-key-filter",
        kind=CoreKind.OPERATOR,
        module="dau_int32_pair_key_filter",
        source="dau_int32_pair_key_filter.sv",
        description="Key partition filter for 16-byte pair rows (NoC lane splitter).",
        golden="dau_core.operators:filter_bar_rows_by_key",
        bench=f"{_BENCHES}.dau_int32_pair_key_filter_tb",
        resources=ResourceEnvelope(lut=106, ff=330, bram36=0.0, dsp=0),
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-range-partitioner",
        kind=CoreKind.OPERATOR,
        module="dau_int32_range_partitioner",
        source="dau_int32_range_partitioner.sv",
        description="HARP-style range partitioner: fans one scan of 16-byte pair rows to NUM_PARTITIONS key-ordered lanes.",
        golden="dau_core.operators:range_partition_bar_rows",
        bench=f"{_BENCHES}.dau_int32_range_partitioner_tb",
        resources=ResourceEnvelope(lut=374, ff=1124, bram36=0.0, dsp=0),  # OOC N=4
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-bitonic-sorter",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_bitonic_sorter",
        source="dau_int32_bitonic_sorter.sv",
        description="Batch bitonic sorter: up to CAPACITY pair rows ascending by key, stable; overflow reports partition-first.",
        golden="dau_core.operators:sort_bar_rows_by_key",
        bench=f"{_BENCHES}.dau_int32_bitonic_sorter_tb",
        resources=ResourceEnvelope(lut=5970, ff=2285, bram36=0.0, dsp=0),  # OOC CAPACITY=16
        count_port="sorted_count",
        contract_kind=ContractKind.ROW_STREAM,
        row_capacity=16,  # HDL parameter CAPACITY default
        unit_kind=UnitKind.SORT,
    ),
    CoreDefinition(
        name="int32-row-predicate-filter",
        kind=CoreKind.OPERATOR,
        module="dau_int32_row_predicate_filter",
        source="dau_int32_row_predicate_filter.sv",
        description="Row-atomic compound predicate filter over 16-byte quad rows: up to three field/op/constant terms in a two-level AND/OR tree (the q6/q19 WHERE stage).",
        depends=("predicate-pkg",),
        golden="dau_core.operators:filter_rows_by_predicate",
        bench=f"{_BENCHES}.dau_int32_row_predicate_filter_tb",
        resources=ResourceEnvelope(lut=735, ff=516, bram36=0.0, dsp=0),
        count_port="selected_count",
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-row-map-alu",
        kind=CoreKind.OPERATOR,
        module="dau_int32_row_map_alu",
        source="dau_int32_row_map_alu.sv",
        description="Per-row field ALU over 16-byte quad rows: dst = src_a <op> src_b with two field operands (fixed-point q6 revenue via MULSHR).",
        depends=("map-alu-pkg",),
        golden="dau_core.operators:map_row_fields",
        bench=f"{_BENCHES}.dau_int32_row_map_alu_tb",
        resources=ResourceEnvelope(lut=706, ff=315, bram36=0.0, dsp=4),
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-field-sum-aggregation",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_field_sum_aggregation",
        source="dau_int32_field_sum_aggregation.sv",
        description="Batch field-sum aggregation over 16-byte quad rows: one INT64 sum per mask-selected field plus the row count in a single close-out record (q6 = one-bit mask; q1's multi-sum is a wider mask).",
        golden="dau_core.operators:field_sum_rows",
        bench=f"{_BENCHES}.dau_int32_field_sum_aggregation_tb",
        resources=ResourceEnvelope(lut=621, ff=529, bram36=0.0, dsp=0),
        count_port="aggregated_count",
        contract_kind=ContractKind.ROW_STREAM,
        opcodes=(OperationCode.SUM, OperationCode.COUNT),
        opcode_input_types=(LogicalType.INT32,),
        unit_kind=UnitKind.AGGREGATION,
    ),
    CoreDefinition(
        name="int32-grouped-field-sum-aggregation",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_grouped_field_sum_aggregation",
        source="dau_int32_grouped_field_sum_aggregation.sv",
        description="Ordered grouped field-sum aggregation over 16-byte quad rows: consecutive-equal-key groups (key = w0.lo, sort-then-stream) each emit a record of the sign-extended group key, one INT64 sum per mask-selected field, and the row count; one success close-out per batch after the final group (q1's grouped multi-sum shape).",
        golden="dau_core.operators:grouped_field_sum_rows",
        bench=f"{_BENCHES}.dau_int32_grouped_field_sum_aggregation_tb",
        resources=ResourceEnvelope(lut=804, ff=636, bram36=0.0, dsp=0),
        count_port="group_count",
        contract_kind=ContractKind.ROW_STREAM,
        opcodes=(OperationCode.SUM, OperationCode.COUNT),
        opcode_input_types=(LogicalType.INT32,),
        unit_kind=UnitKind.AGGREGATION,
    ),
    CoreDefinition(
        name="int32-stream-aggregation",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_stream_aggregation",
        source="dau_int32_stream_aggregation.sv",
        description="Arrow-lite scalar stream aggregation.",
        depends=("aggregation-pkg", "int32-aggregation-tile"),
        golden="dau_core.operators:aggregate_values",
        bench=f"{_BENCHES}.dau_int32_stream_aggregation_tb",
        resources=ResourceEnvelope(lut=446, ff=400, bram36=0.0, dsp=0),
        contract_kind=ContractKind.ARROW_LITE_DESCRIPTOR,
        opcodes=(OperationCode.MIN, OperationCode.MAX, OperationCode.SUM, OperationCode.COUNT),
        opcode_input_types=(LogicalType.INT32,),
        unit_kind=UnitKind.AGGREGATION,
    ),
    CoreDefinition(
        name="int32-arrow-lite-stream-aggregation",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_arrow_lite_stream_aggregation",
        source="dau_int32_arrow_lite_stream_aggregation.sv",
        description="Arrow-lite single-column aggregation core.",
        depends=("aggregation-pkg",),
        golden="dau_core.operators:aggregate_values",
        bench=f"{_BENCHES}.dau_int32_arrow_lite_stream_aggregation_tb",
        resources=ResourceEnvelope(lut=1874, ff=636, bram36=0.0, dsp=0),
        contract_kind=ContractKind.ROW_STREAM,
        opcodes=(OperationCode.MIN, OperationCode.MAX, OperationCode.SUM, OperationCode.COUNT),
        opcode_input_types=(LogicalType.INT32,),
        unit_kind=UnitKind.AGGREGATION,
    ),
    CoreDefinition(
        name="int32-record-batch-aggregation",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_record_batch_aggregation",
        source="dau_int32_record_batch_aggregation.sv",
        description="Record-batch column aggregation core.",
        depends=("aggregation-pkg",),
        golden="dau_core.operators:aggregate_values",
        bench=f"{_BENCHES}.dau_int32_record_batch_aggregation_tb",
        resources=ResourceEnvelope(lut=2266, ff=2741, bram36=0.0, dsp=0),
        contract_kind=ContractKind.ROW_STREAM,
        opcodes=(OperationCode.MIN, OperationCode.MAX, OperationCode.SUM, OperationCode.COUNT),
        opcode_input_types=(LogicalType.INT32,),
        unit_kind=UnitKind.AGGREGATION,
    ),
    CoreDefinition(
        name="int32-aggregation-tile",
        kind=CoreKind.OPERATOR,
        module="dau_int32_aggregation_tile",
        source="dau_int32_aggregation_tile.sv",
        description="Legacy value/opcode aggregation core (predates the stream+status contract; not runner-attachable). Replaced by int32-record-batch-aggregation / int32-bar-aggregation for new work.",
        depends=("aggregation-pkg",),
        golden="dau_core.operators:aggregate_values",
        bench=f"{_BENCHES}.dau_int32_aggregation_tile_tb",
        resources=ResourceEnvelope(lut=283, ff=194, bram36=0.0, dsp=0),
        legacy=True,
        opcodes=(OperationCode.MIN, OperationCode.MAX, OperationCode.SUM, OperationCode.COUNT),
        opcode_input_types=(LogicalType.INT32,),
        unit_kind=UnitKind.AGGREGATION,
    ),
    # stream infrastructure
    CoreDefinition(
        name="stream-broadcast",
        kind=CoreKind.STREAM_INFRA,
        module="dau_stream_broadcast",
        source="dau_stream_broadcast.sv",
        description="N-way stream fanout with taken-bit bookkeeping.",
        resources=ResourceEnvelope(lut=5, ff=2, bram36=0.0, dsp=0),
    ),
    CoreDefinition(
        name="axi-burst-reader",
        kind=CoreKind.STREAM_INFRA,
        module="dau_axi_burst_reader",
        source="dau_axi_burst_reader.sv",
        description="Pipelined AXI4 burst read master streaming a memory window.",
        resources=ResourceEnvelope(lut=529, ff=340, bram36=0.0, dsp=0),
    ),
    CoreDefinition(
        name="axi-record-writer",
        kind=CoreKind.STREAM_INFRA,
        module="dau_axi_record_writer",
        source="dau_axi_record_writer.sv",
        description="AXI4 burst write master draining one record stream to a region.",
        resources=ResourceEnvelope(lut=406, ff=1266, bram36=0.0, dsp=0),
    ),
    CoreDefinition(
        name="axi-write-mux",
        kind=CoreKind.STREAM_INFRA,
        module="dau_axi_write_mux",
        source="dau_axi_write_mux.sv",
        description="N:1 AXI write-channel mux, round-robin, locked AW to B.",
        resources=ResourceEnvelope(lut=133, ff=4, bram36=0.0, dsp=0),
    ),
    # shell adapters / runners
    CoreDefinition(
        name="bram-stream-job",
        kind=CoreKind.SHELL_ADAPTER,
        module="dau_bram_stream_job",
        source="dau_bram_stream_job.sv",
        description="BRAM-staged tile-agnostic job runner.",
        resources=ResourceEnvelope(lut=104, ff=232, bram36=0.0, dsp=0),
    ),
    CoreDefinition(
        name="axi-stream-job",
        kind=CoreKind.SHELL_ADAPTER,
        module="dau_axi_stream_job",
        source="dau_axi_stream_job.sv",
        description="DDR-staged AXI4 master job runner (reader + writer halves fused).",
        bench=f"{_BENCHES}.dau_axi_bar_job_tb",
        resources=ResourceEnvelope(lut=3341, ff=9712, bram36=0.0, dsp=0),
    ),
    CoreDefinition(
        name="identity-registers",
        kind=CoreKind.SHELL_ADAPTER,
        module="dau_identity_registers",
        source="dau_identity_registers.sv",
        description="Identity/capability register block.",
        resources=ResourceEnvelope(lut=45, ff=0, bram36=0.0, dsp=0),
    ),
    CoreDefinition(
        name="identity-axil",
        kind=CoreKind.SHELL_ADAPTER,
        module="dau_identity_axil",
        source="dau_identity_axil.v",
        description="AXI-Lite identity shell adapter.",
    ),
    # sim-only models
    CoreDefinition(
        name="axi-ram-sim",
        kind=CoreKind.SIM_MODEL,
        module="dau_axi_ram_sim",
        source="dau_axi_ram_sim.sv",
        description="Behavioral AXI4 RAM with pipelined ARs, back-to-back bursts, stall injection, backdoor.",
    ),
)

_BY_NAME = {core.name: core for core in CORES}
_BY_MODULE = {core.module: core for core in CORES}


class UnknownCoreError(KeyError):
    pass


def core(name: str) -> CoreDefinition:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise UnknownCoreError(f"unknown core {name!r}; known: {sorted(_BY_NAME)}") from None


def core_for_module(module: str) -> CoreDefinition:
    try:
        return _BY_MODULE[module]
    except KeyError:
        raise UnknownCoreError(f"no core defines module {module!r}") from None


def cores_of_kind(kind: CoreKind) -> tuple[CoreDefinition, ...]:
    return tuple(definition for definition in CORES if definition.kind == kind)


def selectable_operators() -> tuple[CoreDefinition, ...]:
    """Operator tiles a planner or new composition may pick — legacy cores
    (predating the stream+status contract) are excluded. Legacy tiles stay
    in the registry, verified against their goldens, but are not offered
    for new work."""
    return tuple(definition for definition in cores_of_kind(CoreKind.OPERATOR) if not definition.legacy)


def sources_for(*names: str) -> tuple[Path, ...]:
    """Dependency-closed, deduplicated source paths for a set of cores —
    packages and dependencies first, in registry order."""
    selected: dict[str, CoreDefinition] = {}

    def visit(name: str) -> None:
        definition = core(name)
        for dependency in definition.depends:
            visit(dependency)
        selected.setdefault(definition.name, definition)

    for name in names:
        visit(name)
    ordered = [definition for definition in CORES if definition.name in selected]
    return tuple(definition.source_path() for definition in ordered)

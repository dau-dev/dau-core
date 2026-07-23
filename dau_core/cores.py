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

from dataclasses import dataclass, field
from enum import Enum
from importlib import import_module
from pathlib import Path
from typing import Mapping

from .design import UnitKind
from .stream import LogicalType, OperationCode


class CoreKind(Enum):
    OPERATOR = "operator"
    STREAM_INFRA = "stream-infra"
    SHELL_ADAPTER = "shell-adapter"
    PACKAGE = "package"
    SIM_MODEL = "sim-model"


class LoadImageKind(Enum):
    """What a load-capable tile's LOAD-phase input stream is (the two-phase
    load: cfg_load=1 streams the build image, cfg_load=0 probes). The
    composition validates image/length-grid rules per kind; a core without
    this marker has no cfg_load port and can never be phase-bound."""

    BITMAP_64 = "bitmap-64"  # KEY_SPACE/64 little-endian 64-bit words (membership bitmap)
    QUAD_ROWS = "quad-rows"  # 16-byte build rows (hash-join key + payload)


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
class ParameterSpec:
    """An overridable SystemVerilog module parameter: its HDL default and the
    constraint a composition override must satisfy — mirroring the RTL's own
    elaboration guards so an invalid override is rejected at composition time,
    never later as an HDL elaboration failure. ``multiple_of`` mirrors sizing
    (the membership bitmap is ``KEY_SPACE/64`` words); ``choices`` pins an
    exact legal set (the quad-stream widths are 64 or 128, nothing else);
    ``power_of_two`` mirrors mask/bit-select decodes (the key-mask dispatcher's
    ``NUM_PARTITIONS``); ``minimum`` mirrors a lower bound (the rolling-moment
    window is ``>= 2`` — a size-1 sample variance is undefined). The HDL is the
    source of truth; this mirrors it."""

    default: int
    multiple_of: int = 1
    choices: tuple[int, ...] | None = None
    power_of_two: bool = False
    minimum: int = 1
    maximum: int | None = None


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
    # the two-phase load capability: what this tile's LOAD-phase input stream
    # is (None = no cfg_load port; the tile can never be phase-bound)
    load_image: LoadImageKind | None = None
    # overridable SystemVerilog module parameters and their HDL defaults (the
    # membership filter's KEY_SPACE, the broadcast hash join's KEY_SPACE, ...).
    # A composition may override a declared parameter to build a non-default
    # core size (the module-parameter channel); an override of an undeclared
    # name is rejected. The values here mirror the .sv parameter defaults —
    # the HDL is the source of truth, this is the machine-readable declaration
    # the composition validates against.
    parameters: Mapping[str, ParameterSpec] = field(default_factory=dict)

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
        bench=f"{_BENCHES}.dau_int32_keyed_predicate_filter_tb",
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
        bench=f"{_BENCHES}.dau_int32_keyed_map_alu_tb",
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
        name="int32-grouped-field-aggregation",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_grouped_field_aggregation",
        source="dau_int32_grouped_field_aggregation.sv",
        description=(
            "Generic multi-field ordered close-out grouped aggregation: over quad rows (key, v0, v1, v2) grouped by consecutive-equal key, computes first/last/min/max/sum (INT64) for EACH of the three "
            "data fields in one pass, plus the row count — the N-field generalization of int32-grouped-aggregation. Workload-agnostic: OHLCV bars are a field/stat SELECTION over the 11-word record "
            "(open/high/low/close = field0 first/max/min/last, volume = sum field1, ...) via dau_core.operators.bars_from_field_aggregate, so the finance column assignment lives in the host/select, not the tile. "
            "A reappearing key opens a new partial group (host-merged, the partial-group contract); a row torn by input_last on w0 is ERR_STREAM."
        ),
        golden="dau_core.operators:grouped_field_aggregate_rows",
        bench=f"{_BENCHES}.dau_int32_grouped_field_aggregation_tb",
        resources=ResourceEnvelope(lut=910, ff=886, bram36=0.0, dsp=0),  # OOC xc7a200tfbg484-2, Vivado 2025.2, 8 ns met (WNS +3.846)
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
        name="int32-asof-backward",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_asof_backward",
        source="dau_int32_asof_backward.sv",
        description=(
            "Generic keyed backward as-of join: over one host-merged (key, time)-sorted stream of BUILD and PROBE records (build-before-probe at equal key/time), attaches to each probe the most "
            "recent build for the same key with build_time <= probe_time, optionally within cfg_tolerance on the time axis. Each 3-word record is w0={rec_type, key} (rec_type bit0: 0=build/1=probe), "
            "w1=time[63:0], w2={payload_b, payload_a} (two opaque INT32 fields); because the stream is key-grouped the tile holds ONE current build, reset on key change (the key-change shape) — bounded "
            "state, one pass, no per-key table. A matched probe emits a 4-word joined row (time, {probe_a, key}, {build_b, build_a}, {_, probe_b}); a miss (no live build, or older than tolerance) drops, so "
            "the tile realizes matched joins only. The temporal-finance backward as-of is this join with quote=build {bid, ask}, trade=probe {price, size}, key=symbol — the finance vocabulary lives in the "
            "host merge, not the tile. A record torn by input_last before its third word is ERR_STREAM."
        ),
        golden="dau_core.operators:keyed_asof_join_rows",
        bench=f"{_BENCHES}.dau_int32_asof_backward_tb",
        resources=ResourceEnvelope(lut=606, ff=856, bram36=0.0, dsp=0),  # OOC xc7a200tfbg484-2, Vivado 2025.2, 8 ns met (WNS +1.679)
        count_port="match_count",
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-rolling-moments",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_rolling_moments",
        source="dau_int32_rolling_moments.sv",
        description=(
            "Per-symbol rolling-moment tile (the roll stage of the temporal workflow, over the reduced bar/feature stream). Over a count-based window of the last WINDOW values per symbol, "
            "emits the deferred-division moment record (count, sum, sum-of-squares) for every FULL window — one tile serving rolling sum (=sum), mean (=sum/count) and sample variance "
            "(=(count*sum_sq - sum*sum)/(count*(count-1)), the host dividing once). One 32-bit value per row (w0 = {value, symbol}); because the host-sorted stream is symbol-grouped the tile holds ONE "
            "window (a depth-WINDOW shift register) reset on symbol change — the bar tile's key-change shape, no per-symbol table. Warm-up rows (window not yet full) are dropped, matching the None entries "
            "of dau_core.temporal.rolling_moments/rolling_sum. The moment update pipelines over four beats (READ->MUL->DIFF->UPDATE) keeping the 32x32 DSP products and the difference/accumulate off one path; "
            "one record is buffered so the final emitted record carries output_last. WINDOW >= 2 (a size-1 sample variance is undefined; the compiler residues it)."
        ),
        golden="dau_core.temporal:rolling_moments",
        bench=f"{_BENCHES}.dau_int32_rolling_moments_tb",
        resources=ResourceEnvelope(lut=683, ff=1001, bram36=0.0, dsp=8),  # OOC WINDOW=4 xc7a200tfbg484-2, Vivado 2025.2, 8 ns met (WNS +3.155)
        count_port="moment_count",
        contract_kind=ContractKind.ROW_STREAM,
        parameters={"WINDOW": ParameterSpec(default=4, minimum=2)},  # count-based window depth (>= 2); the shift-register depth
    ),
    CoreDefinition(
        name="int32-market-feature",
        kind=CoreKind.OPERATOR,
        success_close_out=True,
        module="dau_int32_market_feature",
        source="dau_int32_market_feature.sv",
        description=(
            "Market-feature map (the derive stage of the temporal workflow): a stateless per-row map from an aligned market row (as-of tile output, 4 words: time, {price, symbol}, {ask, bid}, "
            "{_, size}) to a 7-word feature record — midpoint = (bid+ask)>>1 (33-bit sum, no int32 overflow), spread = ask-bid (INT64, the int32 range can overflow), quote-rule trade_sign "
            "(+1 above midpoint / -1 below / 0 at), signed_volume = trade_sign*size (INT64), dollar_volume = price*size (INT64, the VWAP numerator). One output row per input row; matches "
            "dau_core.temporal.market_features. In the fused "
            "datapath this is a chain stage between the as-of tile and the bar tile — its signed_volume is the value the bar tile accumulates. A row torn by input_last before its fourth word is ERR_STREAM."
        ),
        golden="dau_core.temporal:market_features",
        bench=f"{_BENCHES}.dau_int32_market_feature_tb",
        resources=ResourceEnvelope(lut=480, ff=528, bram36=0.0, dsp=4),  # OOC xc7a200tfbg484-2, Vivado 2025.2, 8 ns met (WNS +0.680)
        count_port="feature_count",
        contract_kind=ContractKind.ROW_STREAM,
    ),
    CoreDefinition(
        name="int32-time-bucket-key",
        kind=CoreKind.OPERATOR,
        module="dau_int32_time_bucket_key",
        source="dau_int32_time_bucket_key.sv",
        description=(
            "Generic time-bucket key rewrite: replaces a row's partition key with a composite (partition, time-bucket) group key so a downstream key-grouped aggregator (bar / grouped-sum) produces "
            "fixed-width TIME-WINDOWED groups per partition — workload-agnostic (finance time bars, TSBS window shapes, ...); the aggregation and any per-row derivation stay in their own tiles. "
            "bucket = time >>> cfg_bucket_shift (window = 2**shift); key = {partition[PART_BITS-1:0], bucket[BUCKET_BITS-1:0]} (PART_BITS = 32 - BUCKET_BITS) so it changes exactly on a partition-or-bucket "
            "boundary (the stream is pre-sorted by (partition, time)); the downstream recovers partition = key >>> BUCKET_BITS. Contract: the partition id fits PART_BITS bits and the host-rebased bucket "
            "fits BUCKET_BITS, so they never collide — ENFORCED by the tile (a partition or bucket that overflows its width is ERR_RANGE, never a silent alias). A mid-lane transform: silent on success "
            "(1:1 rows, output_last rides the last row), status on a torn row (ERR_STREAM) or an out-of-range key (ERR_RANGE); payload passes through."
        ),
        golden="dau_core.operators:time_bucket_key_rows",
        bench=f"{_BENCHES}.dau_int32_time_bucket_key_tb",
        resources=ResourceEnvelope(lut=291, ff=305, bram36=0.0, dsp=0),  # OOC BUCKET_BITS=16 xc7a200tfbg484-2, Vivado 2025.2, 8 ns met (WNS +2.175)
        contract_kind=ContractKind.ROW_STREAM,
        parameters={"BUCKET_BITS": ParameterSpec(default=16, minimum=1, maximum=31)},  # composite key split (PART_BITS = 32 - BUCKET_BITS)
    ),
    CoreDefinition(
        name="int32-pair-key-filter",
        kind=CoreKind.OPERATOR,
        module="dau_int32_pair_key_filter",
        source="dau_int32_pair_key_filter.sv",
        description="Key partition filter for 16-byte pair rows (NoC lane splitter).",
        golden="dau_core.operators:filter_bar_rows_by_key",
        bench=f"{_BENCHES}.dau_int32_pair_key_filter_tb",
        resources=ResourceEnvelope(lut=182, ff=331, bram36=0.0, dsp=0),
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
        name="int32-key-mask-dispatcher",
        kind=CoreKind.OPERATOR,
        module="dau_int32_key_mask_dispatcher",
        source="dau_int32_key_mask_dispatcher.sv",
        description="Key-mask dispatcher: fans one scan of 16-byte quad rows to NUM_PARTITIONS lanes by the key's low bits (dest = key & (NUM_PARTITIONS-1), a power of two) — the broadcast + per-lane pair-key-filter shape folded into one shared router, so each row is delivered once instead of N times. EXACT_BRANCH=1 selects the exact-branch-id mode (q19's disjunct branches): dest = key - 1 for keys in [1, NUM_PARTITIONS], every other key DROPPED at the front for zero lane cycles (no power-of-two requirement). IN_WIDTH (the rows/cycle axis) selects the input framing: 64 (default) accepts two-beat quad rows; 128 accepts one whole row per beat ({w1, w0}) — paired with the wide row-unpack (OUT_WIDTH=128) the shared front sustains 1 row/cycle while the per-lane outputs stay the 64-bit two-beat framing the operator tiles consume (no operator widens). Same-lane runs throttle on the lane's two-beat drain (single-row skid, head-of-line); mask-distributed keys sustain the full front rate.",
        golden="dau_core.operators:mask_dispatch_rows",
        bench=f"{_BENCHES}.dau_int32_key_mask_dispatcher_tb",
        parameters={
            # mask decode dest = key & (N-1) needs a power of two; the exact-branch
            # mode does not — the elaboration guard enforces the conditional pairing
            # the spec cannot express
            "NUM_PARTITIONS": ParameterSpec(default=4),
            "IN_WIDTH": ParameterSpec(default=64, choices=(64, 128)),  # the quad-stream widths
            "EXACT_BRANCH": ParameterSpec(default=0, choices=(0, 1)),  # 1 = exact-branch-id route/drop
        },
        resources=ResourceEnvelope(
            lut=330, ff=1124, bram36=0.0, dsp=0
        ),  # OOC N=4 IN_WIDTH=64, 8ns met (WNS +5.003); the wide front (IN_WIDTH=128) measures SMALLER (298 LUT / 1049 FF, WNS +5.397) — no w0 latch stage. FF-heavy = 4 single-row lane skids (held + emit row registers), same shape as the range partitioner.
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
        resources=ResourceEnvelope(lut=703, ff=514, bram36=0.0, dsp=0),
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
        name="int32-row-unpack",
        kind=CoreKind.OPERATOR,
        # front-end feed transform: silent on success (a mid-lane feed stage
        # ahead of the operators, whose close-out is authoritative); raises
        # status only for an invalid field-descriptor config (ERR_CONFIG). No
        # unit_kind: like the other pure stream transforms (pair-key filter,
        # range partitioner, row filter/map) it is capacity plumbing, not a
        # counted aggregation/join/membership/sort unit in the design inventory.
        module="dau_int32_row_unpack",
        source="dau_int32_row_unpack.sv",
        description="Row-unpack front-end feed over <=64-bit packed rows: bit-extracts four configured fields (offset/width/signed) from one packed word and widens them into the standard 16-byte int32 quad row (field 0 = key = w0.lo) the operator tiles consume. The compact-row feed answer to the DDR-byte-bandwidth-bound scan (~2x rows/sec off DDR, half the host DMA, operators unchanged). Covers q6 (59-bit) and q1 (63-bit) packings. The batch-latched cfg_bypass config port selects the mode: 0 = unpack (packed word in, quad row out); 1 = a zero-added-latency passthrough of pre-widened 16-byte quad rows (two words per row, field descriptors unused), so ONE bitstream benchmarks packed vs unpacked rows on the same silicon and isolates the host packing overhead. OUT_WIDTH (the rows/cycle axis) selects the quad-stream framing: 64 (default) emits two 64-bit beats per row; 128 emits one whole quad row per beat ({w1, w0}), lifting the shared-front emit cap from ~1 row per 2 cycles to 1 row/cycle (silicon showed the 2-beat emit, not the DDR, gates the packed feed) — bypass then gearboxes each two-word quad into one beat. FOLLOW-UP: the scan composition must stage packed rows + place this after the burst reader before broadcast/partition, and the flywheel needs a pack-schema deriving field offsets/widths from column ranges.",
        golden="dau_core.operators:unpack_rows",
        bench=f"{_BENCHES}.dau_int32_row_unpack_tb",
        parameters={"OUT_WIDTH": ParameterSpec(default=64, choices=(64, 128))},  # the quad-stream widths (mirrors the elaboration guard)
        resources=ResourceEnvelope(
            lut=1509, ff=194, bram36=0.0, dsp=0
        ),  # OOC 8ns met (WNS +1.943); LUT-heavy = 4 configurable 64-bit barrel extractors. Re-measured with cfg_bypass: the passthrough mux, mode latch, and torn-row detection pack into existing slice slack (unchanged).
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
        name="int32-key-membership-filter",
        kind=CoreKind.OPERATOR,
        # silent on success in the probe phase (a legal mid-lane chain stage
        # before an aggregation terminal); the zero-forwarded-row compaction
        # and the load-done both close out, like the other filter tiles
        module="dau_int32_key_membership_filter",
        source="dau_int32_key_membership_filter.sv",
        description="Key-set membership filter over 16-byte quad rows: the join tier's entry. A build set is loaded into an exact direct-mapped bitmap (BRAM) in a load phase (the two-phase membership job), then whole rows keep/drop by whether the row key (w0.lo) is a member; cfg_mode selects semi (keep members: IN / semi-join / distinct-probe) vs anti (keep non-members: NOT IN / anti-join). Exact — no false positives (bloom is a reserved future kind).",
        golden="dau_core.operators:filter_rows_by_membership",
        bench=f"{_BENCHES}.dau_int32_key_membership_filter_tb",
        resources=ResourceEnvelope(lut=363, ff=413, bram36=1.0, dsp=0),  # OOC KEY_SPACE=4096; bitmap is one RAMB36, 8 ns met
        count_port="selected_count",
        contract_kind=ContractKind.ROW_STREAM,
        unit_kind=UnitKind.MEMBERSHIP,
        load_image=LoadImageKind.BITMAP_64,
        parameters={"KEY_SPACE": ParameterSpec(default=4096, multiple_of=64)},  # dictionary-id bitmap; KEY_SPACE/64 words
    ),
    CoreDefinition(
        name="int32-broadcast-hash-join",
        kind=CoreKind.OPERATOR,
        # silent on success in the probe phase (a mid-lane join stage before a
        # downstream terminal); the zero-emitted-row compaction and the
        # load-done both close out, like the membership filter
        module="dau_int32_broadcast_hash_join",
        source="dau_int32_broadcast_hash_join.sv",
        description="Broadcast hash join over 16-byte quad rows: the join tier's payload-emitting Joiner (the q3 join spine). A build side (key, b0, b1, b2) is broadcast into a direct-mapped payload table (BRAM) in a load phase (the two-phase join job), then probe rows (key, v0, v1, v2) inner-join against it — a hit emits one (key, out0, out1, out2) row whose data fields are chosen per cfg_out{0,1,2}_sel from the six sources {b0,b1,b2,v0,v1,v2}, a miss drops. Last-writer-wins on a duplicate build key; a build/probe key outside [0, KEY_SPACE) is ERR_RANGE.",
        golden="dau_core.operators:broadcast_hash_join_rows",
        bench=f"{_BENCHES}.dau_int32_broadcast_hash_join_tb",
        resources=ResourceEnvelope(lut=713, ff=422, bram36=3.0, dsp=0),  # OOC KEY_SPACE=1024; payload table is 3 RAMB36, 8 ns met (WNS +3.421)
        count_port="joined_count",
        contract_kind=ContractKind.ROW_STREAM,
        unit_kind=UnitKind.JOIN,
        load_image=LoadImageKind.QUAD_ROWS,
        parameters={"KEY_SPACE": ParameterSpec(default=1024)},  # direct-mapped payload table, clog2(KEY_SPACE)-addressed
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

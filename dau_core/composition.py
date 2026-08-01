"""Composition specs: shell datapaths described as data.

A scan composition is the one-scan-N-lanes shape proven by the broadcast
NoC shell: an AXI burst reader fans a row stream to N lanes, each an
optional partition filter feeding an operator tile and a record writer
with its own output region. The spec names registry cores and their
config-port bindings; a generator walks it to emit the shell top, and
``estimated_resources`` sums the registry's measured envelopes — the
planner-facing cost of a design before it is built.

N=1 with no filter degenerates to the single-tile DDR scan.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from fractions import Fraction
from itertools import pairwise

from ccflow import BaseModel
from pydantic import ConfigDict, StrictInt

from .cores import ContractKind, CoreDefinition, CoreKind, LoadImageKind, ResourceEnvelope, core

SCAN_INFRASTRUCTURE_CORES = ("axi-burst-reader", "stream-broadcast", "axi-record-writer", "axi-write-mux", "identity-registers")

# the only core allowed in the optional front-stage feed transform slot: the
# row-unpack tile that widens packed rows into 16-byte quad rows after the
# burst reader and before the broadcast/partition fan-out.
FRONT_UNPACK_CORE = "int32-row-unpack"
FRONT_GEARBOX_CORE = "record-gearbox"
# the reserved config-binding symbol for the shell's LOAD_PHASE register
# (RegisterOffset.LOAD_PHASE): a tile binding ``cfg_load`` to this symbol is
# phase-switched by the host instead of baked, making the composition
# two-phase (LOAD the build image, then PROBE)
LOAD_PHASE_BINDING = "load_phase"


class CompositionError(Exception):
    # not a ValueError: pydantic wraps ValueError subclasses raised in
    # model_post_init into ValidationError, and the composition contract is
    # that spec construction raises CompositionError directly
    pass


def _validate_tile_params(tile: TileSpec, role: str) -> None:
    """Every module-parameter override on a tile must name a parameter the
    core declares as overridable (``CoreDefinition.parameters``) and carry a
    positive integer value (SV sizes are positive). An unknown name is a
    typo or a param the core does not expose — rejected up front."""
    if not tile.params:
        return
    declared = tile.definition().parameters
    for name, value in tile.params.items():
        if name not in declared:
            known = ", ".join(sorted(declared)) or "none"
            raise CompositionError(f"{role} {tile.core!r} has no overridable parameter {name!r} (declared: {known})")
        spec = declared[name]
        if isinstance(value, bool) or not isinstance(value, int):
            raise CompositionError(f"{role} {tile.core!r} parameter {name!r} must be a positive int, got {value!r}")
        if spec.choices is not None:
            if value not in spec.choices:
                allowed = ", ".join(str(choice) for choice in spec.choices)
                raise CompositionError(f"{role} {tile.core!r} parameter {name!r} must be one of {allowed}, got {value!r}")
            continue  # an enumerated choice set (which may include a 0 flag) is the whole constraint
        if value <= 0:
            raise CompositionError(f"{role} {tile.core!r} parameter {name!r} must be a positive int, got {value!r}")
        if value < spec.minimum:
            raise CompositionError(f"{role} {tile.core!r} parameter {name!r} must be >= {spec.minimum}, got {value!r}")
        if spec.maximum is not None and value > spec.maximum:
            raise CompositionError(f"{role} {tile.core!r} parameter {name!r} must be <= {spec.maximum}, got {value!r}")
        if value % spec.multiple_of != 0:
            raise CompositionError(f"{role} {tile.core!r} parameter {name!r} must be a positive multiple of {spec.multiple_of}, got {value!r}")
        if spec.power_of_two and value & (value - 1):
            raise CompositionError(f"{role} {tile.core!r} parameter {name!r} must be a power of two, got {value!r}")


class TileSpec(BaseModel):
    """One registry core plus its config-port bindings (SystemVerilog
    literals or expressions in terms of the top's signals) and any
    module-parameter overrides (``params``: SystemVerilog PARAMETERs, e.g.
    the membership core's ``KEY_SPACE``, that size the built RTL). Config
    ports are runtime bindings; params are elaboration-time sizes."""

    model_config = ConfigDict(frozen=True)

    core: str
    config: Mapping[str, str] = {}
    # StrictInt: lax coercion (True -> 1, "4" -> 4) would silently satisfy the
    # positive-int parameter contract before _validate_tile_params ever ran
    params: Mapping[str, StrictInt] = {}

    def definition(self) -> CoreDefinition:
        return core(self.core)


class LaneSpec(BaseModel):
    """One lane of the scan: an optional row-atomic partition filter, an
    optional ordered ``chain`` of mid-lane operator stages
    (filter -> map -> ...), and the terminal operator tile. Chain stages
    speak the stream+status contract but need no status counter; the
    terminal tile's count port backs the lane register block."""

    model_config = ConfigDict(frozen=True)

    tile: TileSpec
    partition: TileSpec | None = None
    chain: tuple[TileSpec, ...] = ()


class ScanCompositionSpec(BaseModel):
    """One scan fanned to ``len(lanes)`` lanes with per-lane output
    regions and the NoC lane register block.

    Two fan-out shapes: the default broadcasts the scan to every lane
    (each lane's optional ``partition`` filter selects its rows), or a
    shared ``partitioner`` (e.g. the range partitioner) routes each row
    to exactly one lane — lanes then carry no per-lane partition.

    An optional ``front_unpack`` feed transform (the row-unpack core) sits
    between the burst reader and the fan-out: it widens PACKED rows (half
    the DDR bytes) into the standard 16-byte quad rows, so the fan-out
    still sees the clean 32-bit key it partitions on. It coexists with
    either fan-out shape.

    An optional ``front_gearbox`` sits in the same feed position but
    reframes rather than widens: it turns wide beats into ONE WHOLE
    RECORD per cycle, so the fan-out behind it routes records instead
    of rows. Its bus is therefore a record size, not a row width."""

    model_config = ConfigDict(frozen=True)

    name: str
    lanes: tuple[LaneSpec, ...]
    burst_beats: int = 32
    addr_width: int = 32
    # the memory read datapath width (the width-tier catalog knob, the
    # build-level ``width=`` selection): the burst reader's DATA_WIDTH and,
    # in a dispatch-shaped scan, the row framing into the fan-out. 64 keeps
    # the classic single shared M_AXI; wider tiers split a wide read-only
    # master off from the 64-bit record-write master in the walker.
    data_width: int = 64
    partitioner: TileSpec | None = None
    front_unpack: TileSpec | None = None
    # THE RECORD GEARBOX: reframes wide beats into whole records so the
    # fan-out behind it consumes one record per cycle instead of one word.
    # It is declared HERE, in the composition, rather than as a build-time
    # flag because it is a core the design INSTANTIATES: it has to reach
    # ``core_names`` (which stages the HDL Vivado compiles) and the
    # canonical spec (which keys the design cache). A geared and an
    # ungeared design are different bitstreams and must not share a hash.
    front_gearbox: TileSpec | None = None
    # THE FUSED CHAIN: allow terminal-shaped tiles (a join, an aggregator) to
    # sit MID-LANE so their intermediate records never touch DDR — the
    # avoided-materialization shape. The lane still presents exactly one
    # close-out: each fused stage's success status is drained the cycle it
    # appears and only its ERRORS latch into the lane status (the walker emits
    # the protocol; the latch clears per job). Declared, never inferred: a
    # chain that fuses close-outs reads differently at the register level, so
    # the composition says so out loud.
    fused_chain: bool = False

    def model_post_init(self, context) -> None:
        # composition validation raises CompositionError directly (the public
        # contract consumers catch); model_post_init propagates it unwrapped,
        # unlike a pydantic validator
        super().model_post_init(context)
        if not self.lanes:
            raise CompositionError("a scan composition needs at least one lane")
        if self.data_width not in (64, 128, 256, 512):
            raise CompositionError(f"data_width must be 64/128/256/512, got {self.data_width}")
        wide_lane = self.lanes[0].tile.definition().contract_kind is ContractKind.WIDE_ROW_STREAM
        if wide_lane:
            # THE WIDE LANE: reader beats wire straight into a chain of
            # wide-row-stream stages — the lane IS the fan-out (SLOTS rows
            # per cycle), so no partitioner/broadcast is involved at all
            if len(self.lanes) != 1:
                raise CompositionError("a wide lane is the whole datapath: exactly one lane (no wide fan-out exists)")
            if self.partitioner is not None:
                raise CompositionError("a wide lane consumes the reader stream directly; a shared partitioner cannot feed it")
            if self.front_unpack is not None:
                raise CompositionError("a wide lane reads whole quad rows; the packed front unpacker (64-bit packed rows) cannot feed it")
            lane = self.lanes[0]
            if lane.partition is not None:
                raise CompositionError("a wide lane takes no per-lane partition filter (it sees every row by construction)")

            def _slots_of(tile: TileSpec, role: str) -> int:
                if tile.definition().contract_kind is not ContractKind.WIDE_ROW_STREAM:
                    raise CompositionError(
                        f"{role} {tile.core!r} speaks the 64-bit row-stream contract inside a wide lane; every stage of a "
                        "wide lane must be a wide-row-stream core (mixed framings cannot wire)"
                    )
                declared = tile.definition().parameters.get("SLOTS")
                if declared is None:
                    raise CompositionError(f"{role} {tile.core!r} declares no SLOTS parameter")
                return int(tile.params.get("SLOTS", declared.default))

            slots = _slots_of(lane.tile, "wide lane terminal")
            for stage in lane.chain:
                stage_slots = _slots_of(stage, "wide lane chain stage")
                if stage_slots != slots:
                    raise CompositionError(f"wide lane SLOTS disagree: chain stage {stage.core!r} has {stage_slots}, the terminal has {slots}")
            if self.data_width != slots * 128:
                raise CompositionError(
                    f"a wide lane at SLOTS={slots} consumes {slots * 128}-bit beats; data_width={self.data_width} does not match "
                    "(set data_width = SLOTS x 128)"
                )
        if self.data_width != 64 and not wide_lane:
            if self.front_unpack is not None:
                raise CompositionError(
                    "a packed front unpacker reads 64-bit packed rows; wide packed reads (data_width > 64) are a follow-up — "
                    "drop the front_unpack to feed quad rows at data_width, or keep data_width=64"
                )
            if self.partitioner is None:
                raise CompositionError(f"a {self.data_width}-bit feed needs a shared partitioner/dispatcher; the stream broadcast is 64-bit only")
        if self.partitioner is not None:
            definition = self.partitioner.definition()
            if definition.kind != CoreKind.OPERATOR:
                raise CompositionError(f"partitioner {self.partitioner.core!r} is not an operator")
            if definition.legacy:
                raise CompositionError(f"partitioner {self.partitioner.core!r} is legacy; not for new compositions")
            if definition.success_close_out:
                raise CompositionError(
                    f"partitioner {self.partitioner.core!r} emits a success close-out per batch; the lane status mux allows exactly one — "
                    "only the terminal tile may close out on success"
                )
            if definition.contract_kind is ContractKind.WIDE_ROW_STREAM:
                raise CompositionError(
                    f"partitioner {self.partitioner.core!r} speaks the wide-row-stream contract; the shared router slot is not a wide chain stage"
                )
            _validate_tile_params(self.partitioner, "partitioner")
            # the key-mask dispatcher's mask decode (dest = key & (N-1)) needs
            # a power-of-two partition count; the exact-branch mode does not —
            # the conditional pairing the per-parameter spec cannot express.
            # The effective count is the lane count (the walker derives
            # NUM_PARTITIONS from it) unless a params override names it.
            # the framing widths must AGREE end to end: the feed (data_width,
            # or the unpacker's OUT_WIDTH when packed) frames the rows the
            # partitioner ingests, so a partitioner declaring IN_WIDTH must
            # match it — and one that declares no IN_WIDTH speaks 64-bit
            # two-beat framing only (a wide feed would misframe rows)
            feed_width = self.data_width
            if self.front_unpack is not None:
                out_spec = self.front_unpack.definition().parameters.get("OUT_WIDTH")
                feed_width = self.front_unpack.params.get("OUT_WIDTH", out_spec.default if out_spec else 64)
            # whole-record framing: the partitioner's IN_WIDTH is the RECORD
            # bus, not the feed. A record gearbox in front owns the
            # reconciliation between the reader's beats and that bus (and the
            # RTL elaboration-guards IN_WIDTH == RECORD_WORDS*64), so the
            # feed-framing rule below does not apply.
            record_input = self.partitioner.params.get("RECORD_INPUT", 0) == 1
            in_spec = None if record_input else definition.parameters.get("IN_WIDTH")
            if in_spec is None:
                if feed_width != 64 and not record_input:
                    raise CompositionError(
                        f"partitioner {self.partitioner.core!r} declares no IN_WIDTH (64-bit framing only) but the feed frames {feed_width}-bit rows"
                    )
            else:
                in_width = self.partitioner.params.get("IN_WIDTH", in_spec.default)
                if in_width != feed_width:
                    raise CompositionError(
                        f"partitioner {self.partitioner.core!r} IN_WIDTH={in_width} does not match the {feed_width}-bit feed framing "
                        "(data_width, or the front unpacker's OUT_WIDTH)"
                    )
            if self.partitioner.core == "int32-key-mask-dispatcher" and self.partitioner.params.get("EXACT_BRANCH", 0) != 1:
                partitions = self.partitioner.params.get("NUM_PARTITIONS", len(self.lanes))
                if partitions & (partitions - 1):
                    raise CompositionError(
                        f"partitioner {self.partitioner.core!r} 'NUM_PARTITIONS' must be a power of two in mask mode, got {partitions!r}"
                    )
        if self.front_gearbox is not None:
            if self.front_gearbox.core != FRONT_GEARBOX_CORE:
                raise CompositionError(f"front gearbox {self.front_gearbox.core!r} is not the {FRONT_GEARBOX_CORE!r} reframing core")
            _validate_tile_params(self.front_gearbox, "front gearbox")
            if self.data_width <= 64:
                raise CompositionError("front_gearbox requires data_width > 64: a 64-bit feed already delivers one word per cycle")
        if self.front_unpack is not None:
            if self.front_unpack.core != FRONT_UNPACK_CORE:
                raise CompositionError(
                    f"front unpacker {self.front_unpack.core!r} is not the {FRONT_UNPACK_CORE!r} feed transform; the front stage widens "
                    "packed rows after the burst reader and before the fan-out, a role only the row-unpack core fills"
                )
            _validate_tile_params(self.front_unpack, "front unpacker")
            if self.partitioner is None:
                out_spec = self.front_unpack.definition().parameters.get("OUT_WIDTH")
                unpack_width = self.front_unpack.params.get("OUT_WIDTH", out_spec.default if out_spec else 64)
                if unpack_width != 64:
                    raise CompositionError(
                        f"front unpacker OUT_WIDTH={unpack_width} feeds the stream broadcast, which is 64-bit only — "
                        "wide unpacked framing needs a shared partitioner/dispatcher (IN_WIDTH to match)"
                    )
        for lane in self.lanes:
            if not wide_lane:
                for slot_tile, slot_role in (
                    (lane.tile, "lane tile"),
                    (lane.partition, "lane partition"),
                    *((stage, "lane chain stage") for stage in lane.chain),
                ):
                    if slot_tile is None:
                        continue
                    if slot_tile.definition().contract_kind is ContractKind.WIDE_ROW_STREAM:
                        raise CompositionError(
                            f"{slot_role} {slot_tile.core!r} speaks the wide-row-stream contract; this lane's fan-out delivers 64-bit "
                            "two-beat rows — a wide core rides only the wide-lane shape (a lane whose TERMINAL is wide)"
                        )
            tile = lane.tile.definition()
            if tile.kind != CoreKind.OPERATOR:
                raise CompositionError(f"lane tile {lane.tile.core!r} is {tile.kind.value}, not an operator")
            if tile.legacy:
                raise CompositionError(f"lane tile {lane.tile.core!r} is legacy (predates the stream+status contract); not for new compositions")
            if tile.count_port is None:
                raise CompositionError(f"lane tile {lane.tile.core!r} does not expose a status counter")
            if not tile.success_close_out:
                raise CompositionError(
                    f"lane tile {lane.tile.core!r} is silent on success; the record writer's done waits on a consumed close-out, "
                    "so the terminal tile must emit one per batch"
                )
            _validate_tile_params(lane.tile, "lane tile")
            if lane.partition is not None and self.partitioner is not None:
                raise CompositionError(f"lane tile {lane.tile.core!r} carries a partition filter but the scan already has a shared partitioner")
            if lane.partition is not None:
                partition = lane.partition.definition()
                if partition.kind != CoreKind.OPERATOR:
                    raise CompositionError(f"lane partition {lane.partition.core!r} is not an operator")
                if partition.legacy:
                    raise CompositionError(
                        f"lane partition {lane.partition.core!r} is legacy (predates the stream+status contract); not for new compositions"
                    )
                if partition.success_close_out:
                    raise CompositionError(
                        f"lane partition {lane.partition.core!r} emits a success close-out per batch; the lane status mux allows exactly one — "
                        "only the terminal tile may close out on success"
                    )
                _validate_tile_params(lane.partition, "lane partition")
            for stage in lane.chain:
                definition = stage.definition()
                if definition.kind != CoreKind.OPERATOR:
                    raise CompositionError(f"lane chain stage {stage.core!r} is {definition.kind.value}, not an operator")
                if definition.legacy:
                    raise CompositionError(
                        f"lane chain stage {stage.core!r} is legacy (predates the stream+status contract); not for new compositions"
                    )
                if definition.success_close_out and not self.fused_chain:
                    raise CompositionError(
                        f"lane chain stage {stage.core!r} emits a success close-out per batch; the lane status mux allows exactly one — "
                        "chain stages must be silent on success, or the composition must declare fused_chain=True (the fused-chain "
                        "protocol: mid-stage close-outs are drained and only their errors reach the lane status)"
                    )
                _validate_tile_params(stage, "lane chain stage")
        self._validate_load_phase_shape()

    def _validate_load_phase_shape(self) -> None:
        """The two-phase load shape (v1): a tile binding ``cfg_load`` to the
        reserved ``load_phase`` symbol is driven by the shell's LOAD_PHASE
        register, making the composition two-phase (a LOAD job streams the
        tile's build image; a PROBE job streams rows). The LOAD image must
        reach every load tile unmolested, so the shape is constrained: every
        lane's ``chain[0]`` is the load tile (uniformly — a loadless lane
        would misread the image as rows), no partition filters, no shared
        partitioner (the broadcast delivers the image to every lane), no
        front unpacker (it would re-slice image words), and — for a BITMAP_64
        load tile — ``KEY_SPACE`` a multiple of 128 so the
        ``KEY_SPACE/64``-word image lands on the 16-byte input length grid
        (QUAD_ROWS images are 16-byte rows, always on the grid)."""

        def binds_load(tile: TileSpec | None) -> bool:
            return tile is not None and tile.config.get("cfg_load") == LOAD_PHASE_BINDING

        def stray(tile: TileSpec | None, role: str) -> None:
            if tile is None:
                return
            for key, value in tile.config.items():
                if value == LOAD_PHASE_BINDING and key != "cfg_load":
                    raise CompositionError(f"{role} {tile.core!r} binds {key!r} to the reserved {LOAD_PHASE_BINDING!r} symbol; only cfg_load may")
            if binds_load(tile) and role != "lane chain stage 0":
                raise CompositionError(
                    f"{role} {tile.core!r} binds cfg_load to {LOAD_PHASE_BINDING!r}; a load tile must be the first chain stage "
                    "(the LOAD image must reach it unmolested)"
                )

        stray(self.partitioner, "partitioner")
        stray(self.front_unpack, "front unpacker")
        stray(self.front_gearbox, "front gearbox")
        for lane in self.lanes:
            stray(lane.tile, "lane tile")
            stray(lane.partition, "lane partition")
            for index, stage in enumerate(lane.chain):
                stray(stage, "lane chain stage 0" if index == 0 else "lane chain stage")

        load_lanes = [lane for lane in self.lanes if lane.chain and binds_load(lane.chain[0])]
        if not load_lanes:
            return
        if len(load_lanes) != len(self.lanes):
            raise CompositionError(
                "a two-phase composition must carry the load tile at chain[0] of EVERY lane: a loadless lane would misread the "
                f"LOAD image as rows ({len(load_lanes)} of {len(self.lanes)} lanes bind cfg_load to {LOAD_PHASE_BINDING!r})"
            )
        if self.partitioner is not None:
            raise CompositionError("a two-phase composition cannot use a shared partitioner: the LOAD image must broadcast to every lane's load tile")
        if self.front_unpack is not None:
            raise CompositionError("a two-phase composition cannot use a front unpacker: it would re-slice the LOAD image words")
        for lane in self.lanes:
            if lane.partition is not None:
                raise CompositionError(
                    f"a two-phase composition cannot use per-lane partition filters: {lane.partition.core!r} would filter the LOAD image"
                )
            load_tile = lane.chain[0]
            definition = load_tile.definition()
            # load capability is registry truth, not config spelling: only a
            # core declaring its LOAD-phase image kind has a cfg_load port
            if definition.load_image is None:
                raise CompositionError(
                    f"chain stage {load_tile.core!r} binds cfg_load to {LOAD_PHASE_BINDING!r} but declares no load_image kind; "
                    "only load-capable cores (membership filter, broadcast hash join) can be phase-bound"
                )
            if definition.load_image is LoadImageKind.BITMAP_64:
                # the bitmap image is KEY_SPACE/64 64-bit words = KEY_SPACE/8
                # bytes; it lands on the 16-byte input grid iff KEY_SPACE % 128
                key_space = load_tile.params.get("KEY_SPACE", definition.parameters["KEY_SPACE"].default)
                if key_space % 128 != 0:
                    raise CompositionError(
                        f"load tile {load_tile.core!r} KEY_SPACE={key_space} must be a multiple of 128: the KEY_SPACE/64-word LOAD image "
                        "must land on the 16-byte input length grid"
                    )
            # QUAD_ROWS images are 16-byte build rows — always on the grid

    def is_wide_lane(self) -> bool:
        """Whether this is the wide-lane shape: the single lane's terminal
        speaks the wide-row-stream contract, so the reader feeds the lane
        directly — no broadcast, no partitioner (validated at
        construction)."""
        return self.lanes[0].tile.definition().contract_kind is ContractKind.WIDE_ROW_STREAM

    def core_names(self) -> tuple[str, ...]:
        """Every registry core the composition instantiates, infrastructure
        included, deduplicated in first-use order. A shared partitioner
        replaces the broadcast."""
        names: dict[str, None] = {}
        no_broadcast = self.partitioner is not None or self.is_wide_lane()
        for name in SCAN_INFRASTRUCTURE_CORES:
            if no_broadcast and name == "stream-broadcast":
                continue
            names.setdefault(name)
        if self.front_gearbox is not None:
            names.setdefault(self.front_gearbox.core)
        if self.front_unpack is not None:
            names.setdefault(self.front_unpack.core)
        if self.partitioner is not None:
            names.setdefault(self.partitioner.core)
        for lane in self.lanes:
            if lane.partition is not None:
                names.setdefault(lane.partition.core)
            for stage in lane.chain:
                names.setdefault(stage.core)
            names.setdefault(lane.tile.core)
        return tuple(names)


def stream_shape_mismatches(spec: ScanCompositionSpec) -> tuple[str, ...]:
    """Describe every known record-width mismatch without rejecting ``spec``.

    This is detection only: it neither changes composition validity nor inserts
    adapters. Runtime-variable producer widths cannot be compared and are
    intentionally omitted until a concrete output shape exists.
    """
    mismatches: list[str] = []

    def compare(producer: TileSpec, consumer: TileSpec, location: str) -> None:
        emitted = producer.definition().effective_output_record_words(producer.params)
        expected = consumer.definition().effective_record_words(consumer.params)
        if emitted is not None and emitted != expected:
            mismatches.append(
                f"{location}: producer {producer.core!r} emits {emitted}-word records, but consumer {consumer.core!r} expects {expected}-word records"
            )

    front = spec.front_unpack
    if front is not None and spec.partitioner is not None:
        compare(front, spec.partitioner, "front feed")

    lane_feed = spec.partitioner if spec.partitioner is not None else front
    for index, lane in enumerate(spec.lanes):
        stages = [*([] if lane.partition is None else [lane.partition]), *lane.chain, lane.tile]
        if lane_feed is not None:
            compare(lane_feed, stages[0], f"lane {index} feed")
        for producer, consumer in pairwise(stages):
            compare(producer, consumer, f"lane {index}")

    return tuple(mismatches)


def throughput_rows_per_cycle(spec: ScanCompositionSpec, *, stage_pass_fractions: Mapping[int, Sequence[Fraction]] | None = None) -> Fraction:
    """The composed design's GUARANTEED sustained ingest rate, in rows per
    job-clock cycle — the number the scheduler plans on (declared rates are
    floors, so silicon may burst higher; it will not sustain lower). An
    exact rational computed ONLY from public interface facts
    (each core's declared ``ingest`` rate plus the spec's feed shape), so
    the scheduler can rate-match a composition of cores it cannot inspect.

    Three factors, minimum wins:

    - **front delivery**: the reader hands off ``data_width/128`` quad rows
      per cycle (64-bit staging frames one row per two beats = 1/2). A
      ``front_unpack`` feed re-times packed rows at its ``OUT_WIDTH``:
      64 emits one row per two cycles, 128 one per cycle.
    - **shared fan-out**: a partitioner/dispatcher's own declared ingest.
    - **lane consumption**: with a shared partitioner rows split across
      lanes, so lane rates SUM; a broadcast shows every row to every lane,
      so the SLOWEST lane is the system rate (no summing). A lane's rate
      is the minimum declared ingest over its partition, chain, and
      terminal tile.

    DISTRIBUTION ASSUMPTION (explicit, not fine print): the partitioned
    sum assumes rows spread across lanes per the partitioner's routing —
    the key-mask dispatcher's declaration already says "mask-distributed
    keys". Adversarial inputs (every key to one lane) degrade to a single
    lane's rate; a scheduler planning for that regime should use
    ``min(lane rates)`` instead. The broadcast path has no such
    assumption (every lane sees every row regardless of keys).

    SELECTIVITY: by default every stage is charged as if EVERY input row
    reached it, which is the worst case and can be badly pessimistic for
    a chain whose slow stage sits behind a filter. q6 is the worked
    example: its map stage sustains one row per seven cycles standalone
    (bench-measured), but two predicates upstream pass ~2% of rows, so
    the map never binds and silicon ran at 0.483 rows/cycle — 3.4x the
    no-selectivity figure. Pass ``stage_pass_fractions`` (lane index ->
    the fraction of INPUT rows reaching each chain stage, in chain order)
    when the caller knows the plan; each stage's rate is then charged
    against the rows it actually sees. Selectivity is data-dependent, so
    the composition never guesses it — that is the planner's to supply.
    """

    def _tile_rate(tile: TileSpec, role: str) -> Fraction:
        definition = tile.definition()
        ingest = definition.ingest
        if ingest is None:
            raise CompositionError(f"{role} core {tile.core!r} declares no ingest rate (required public interface for stream operators)")
        try:
            return ingest.effective(tile.params, definition.parameters)
        except ValueError as exc:
            raise CompositionError(f"{role} core {tile.core!r}: {exc}") from exc

    if spec.front_unpack is not None:
        # the packed feed: one 8-byte packed row per 64-bit beat from the
        # reader, then the unpacker's own framing-shaped rate
        delivery = min(Fraction(1), _tile_rate(spec.front_unpack, "front unpacker"))
    elif spec.data_width == 64:
        delivery = Fraction(1, 2)
    else:
        delivery = Fraction(spec.data_width, 128)

    supplied = stage_pass_fractions or {}
    if supplied and spec.partitioner is not None and len({tuple(v) for v in supplied.values()}) > 1:
        # under a shared partitioner the lane rates are SUMMED, which only
        # holds if routing can shift load toward the faster lanes. Key-based
        # routing cannot: an evenly-fed slow lane binds the whole feed well
        # below the sum. Rather than model routing, refuse the ambiguous case.
        raise CompositionError(
            "stage_pass_fractions must be identical across partitioned lanes: key routing splits rows evenly, so a lane "
            "with worse selectivity binds the feed and the summed-lane model would overpredict"
        )

    lane_rates = []
    for index, lane in enumerate(spec.lanes):
        # one reach fraction per chain stage PLUS one for the terminal (what
        # LEAVES the final stage), so a chain ending in a filter is not
        # charged the terminal at the wrong point
        passes = [Fraction(f) for f in supplied.get(index, ())]
        if passes and len(passes) != len(lane.chain) + 1:
            raise CompositionError(
                f"lane {index} needs {len(lane.chain) + 1} pass fractions (one per chain stage plus the terminal), got {len(passes)}"
            )
        if any(f < 0 or f > 1 for f in passes):
            raise CompositionError(f"lane {index} pass fractions must lie in [0, 1], got {passes}")
        if not passes:
            passes = [Fraction(1)] * (len(lane.chain) + 1)

        # a stage's rate limits the INPUT stream only in proportion to the
        # rows that reach it: a stage seeing a tenth of the rows can be ten
        # times slower before it binds. A stage nothing reaches cannot bind.
        limits = []
        for position, tile in enumerate([*lane.chain, lane.tile]):
            reaching = passes[position]
            if reaching == 0:
                continue
            role = f"lane {index} terminal" if position == len(lane.chain) else f"lane {index} chain stage"
            limits.append(_tile_rate(tile, role) / reaching)
        if lane.partition is not None:
            limits.append(_tile_rate(lane.partition, f"lane {index} partition"))
        lane_rates.append(min(limits) if limits else _tile_rate(lane.tile, f"lane {index} terminal"))

    if spec.partitioner is not None:
        consumption = min(_tile_rate(spec.partitioner, "partitioner"), sum(lane_rates, Fraction(0)))
    else:
        consumption = min(lane_rates)
    return min(delivery, consumption)


def estimated_resources(spec: ScanCompositionSpec) -> ResourceEnvelope:
    """Sum of the measured envelopes of every instance the composition
    places (per-lane cores counted per lane). ``measured`` is the AND of
    the constituents — one unmeasured core marks the estimate.

    TODO: a param-override'd tile (e.g. membership KEY_SPACE=6_000_000) has a
    variable envelope — BRAM scales with KEY_SPACE (the bitmap is KEY_SPACE
    bits), so this sum, which uses the registry's default-size envelope,
    under-counts BRAM for an up-sized core. The OOC measurement at the real
    KEY_SPACE is the source of truth; this estimate stays a lower bound and,
    critically, does not crash on a param'd tile (the envelope lookup is by
    core name, independent of params)."""
    lut = ff = dsp = 0
    bram = 0.0
    measured = True

    def add(name: str) -> None:
        nonlocal lut, ff, bram, dsp, measured
        envelope = core(name).resources
        if envelope is None:
            measured = False
            return
        lut += envelope.lut
        ff += envelope.ff
        bram += envelope.bram36
        dsp += envelope.dsp
        measured = measured and envelope.measured

    for name in ("axi-burst-reader", "axi-write-mux", "identity-registers"):
        add(name)
    if spec.front_gearbox is not None:
        add(spec.front_gearbox.core)
    if spec.front_unpack is not None:
        add(spec.front_unpack.core)
    if spec.partitioner is not None:
        add(spec.partitioner.core)
    elif not spec.is_wide_lane():
        # the wide lane feeds the single lane directly: no broadcast placed
        add("stream-broadcast")
    for lane in spec.lanes:
        add("axi-record-writer")
        if lane.partition is not None:
            add(lane.partition.core)
        for stage in lane.chain:
            add(stage.core)
        add(lane.tile.core)
    return ResourceEnvelope(lut=lut, ff=ff, bram36=bram, dsp=dsp, measured=measured)


SPEC_CANONICAL_VERSION = "3"  # 3: fused_chain joined the canonical form (the fused-chain protocol, 2026-07-31)


def canonical_spec_json(spec: ScanCompositionSpec) -> str:
    """The canonical serialized form of a composition spec: deterministic
    JSON (sorted keys, compact separators) of the FULL elaborated spec —
    every field, defaults included. The canonical text captures the
    effective design: if a model default ever changes, old and new specs
    produce different text rather than silently sharing a cache key. The
    cost is that growing the spec model changes hashes (a safe cache miss);
    that evolution is the explicit job of SPEC_CANONICAL_VERSION."""
    payload = spec.model_dump(mode="json")
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def spec_hash(spec: ScanCompositionSpec) -> str:
    """The content hash of a composition spec: sha256 over the canonical
    form, prefixed with the canonical-form version so a deliberate change
    to the canonicalization is distinguishable from a different design.
    This is the design-cache key: a planner-emitted design and a resident
    built design that elaborate to the same spec share one hash."""
    digest = hashlib.sha256(canonical_spec_json(spec).encode()).hexdigest()
    return f"{SPEC_CANONICAL_VERSION}:{digest}"


def spec_from_canonical(text: str) -> ScanCompositionSpec:
    """Rebuild a spec from its canonical form. Construction re-validates
    against the registry (kinds, status roles, parameter bounds), so a
    tampered or stale canonical form fails loudly, never builds."""
    return ScanCompositionSpec.model_validate_json(text)

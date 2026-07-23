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

from typing import Mapping

from ccflow import BaseModel

from .cores import CoreDefinition, CoreKind, LoadImageKind, ResourceEnvelope, core

SCAN_INFRASTRUCTURE_CORES = ("axi-burst-reader", "stream-broadcast", "axi-record-writer", "axi-write-mux", "identity-registers")

# the only core allowed in the optional front-stage feed transform slot: the
# row-unpack tile that widens packed rows into 16-byte quad rows after the
# burst reader and before the broadcast/partition fan-out.
FRONT_UNPACK_CORE = "int32-row-unpack"
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

    model_config = {"frozen": True}

    core: str
    config: Mapping[str, str] = {}
    params: Mapping[str, int] = {}

    def definition(self) -> CoreDefinition:
        return core(self.core)


class LaneSpec(BaseModel):
    """One lane of the scan: an optional row-atomic partition filter, an
    optional ordered ``chain`` of mid-lane operator stages
    (filter -> map -> ...), and the terminal operator tile. Chain stages
    speak the stream+status contract but need no status counter; the
    terminal tile's count port backs the lane register block."""

    model_config = {"frozen": True}

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
    either fan-out shape."""

    model_config = {"frozen": True}

    name: str
    lanes: tuple[LaneSpec, ...]
    burst_beats: int = 32
    addr_width: int = 32
    partitioner: TileSpec | None = None
    front_unpack: TileSpec | None = None

    def model_post_init(self, context) -> None:
        # composition validation raises CompositionError directly (the public
        # contract consumers catch); model_post_init propagates it unwrapped,
        # unlike a pydantic validator
        super().model_post_init(context)
        if not self.lanes:
            raise CompositionError("a scan composition needs at least one lane")
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
            _validate_tile_params(self.partitioner, "partitioner")
            # the key-mask dispatcher's mask decode (dest = key & (N-1)) needs
            # a power-of-two partition count; the exact-branch mode does not —
            # the conditional pairing the per-parameter spec cannot express.
            # The effective count is the lane count (the walker derives
            # NUM_PARTITIONS from it) unless a params override names it.
            if self.partitioner.core == "int32-key-mask-dispatcher" and self.partitioner.params.get("EXACT_BRANCH", 0) != 1:
                partitions = self.partitioner.params.get("NUM_PARTITIONS", len(self.lanes))
                if partitions & (partitions - 1):
                    raise CompositionError(
                        f"partitioner {self.partitioner.core!r} 'NUM_PARTITIONS' must be a power of two in mask mode, got {partitions!r}"
                    )
        if self.front_unpack is not None:
            if self.front_unpack.core != FRONT_UNPACK_CORE:
                raise CompositionError(
                    f"front unpacker {self.front_unpack.core!r} is not the {FRONT_UNPACK_CORE!r} feed transform; the front stage widens "
                    "packed rows after the burst reader and before the fan-out, a role only the row-unpack core fills"
                )
            _validate_tile_params(self.front_unpack, "front unpacker")
        for lane in self.lanes:
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
                if definition.success_close_out:
                    raise CompositionError(
                        f"lane chain stage {stage.core!r} emits a success close-out per batch; the lane status mux allows exactly one — "
                        "chain stages must be silent on success"
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

    def core_names(self) -> tuple[str, ...]:
        """Every registry core the composition instantiates, infrastructure
        included, deduplicated in first-use order. A shared partitioner
        replaces the broadcast."""
        names: dict[str, None] = {}
        for name in SCAN_INFRASTRUCTURE_CORES:
            if self.partitioner is not None and name == "stream-broadcast":
                continue
            names.setdefault(name)
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
    if spec.front_unpack is not None:
        add(spec.front_unpack.core)
    if spec.partitioner is not None:
        add(spec.partitioner.core)
    else:
        add("stream-broadcast")
    for lane in spec.lanes:
        add("axi-record-writer")
        if lane.partition is not None:
            add(lane.partition.core)
        for stage in lane.chain:
            add(stage.core)
        add(lane.tile.core)
    return ResourceEnvelope(lut=lut, ff=ff, bram36=bram, dsp=dsp, measured=measured)

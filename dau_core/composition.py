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

from dataclasses import dataclass, field
from typing import Mapping

from .cores import CoreDefinition, CoreKind, ResourceEnvelope, core

SCAN_INFRASTRUCTURE_CORES = ("axi-burst-reader", "stream-broadcast", "axi-record-writer", "axi-write-mux", "identity-registers")


class CompositionError(ValueError):
    pass


@dataclass(frozen=True)
class TileSpec:
    """One registry core plus its config-port bindings (SystemVerilog
    literals or expressions in terms of the top's signals)."""

    core: str
    config: Mapping[str, str] = field(default_factory=dict)

    def definition(self) -> CoreDefinition:
        return core(self.core)


@dataclass(frozen=True)
class LaneSpec:
    """One lane of the scan: an optional row-atomic partition filter ahead
    of the operator tile."""

    tile: TileSpec
    partition: TileSpec | None = None


@dataclass(frozen=True)
class ScanCompositionSpec:
    """One scan fanned to ``len(lanes)`` lanes with per-lane output
    regions and the NoC lane register block.

    Two fan-out shapes: the default broadcasts the scan to every lane
    (each lane's optional ``partition`` filter selects its rows), or a
    shared ``partitioner`` (e.g. the range partitioner) routes each row
    to exactly one lane — lanes then carry no per-lane partition."""

    name: str
    lanes: tuple[LaneSpec, ...]
    burst_beats: int = 32
    addr_width: int = 32
    partitioner: TileSpec | None = None

    def __post_init__(self) -> None:
        if not self.lanes:
            raise CompositionError("a scan composition needs at least one lane")
        if self.partitioner is not None:
            definition = self.partitioner.definition()
            if definition.kind != CoreKind.OPERATOR:
                raise CompositionError(f"partitioner {self.partitioner.core!r} is not an operator")
            if definition.legacy:
                raise CompositionError(f"partitioner {self.partitioner.core!r} is legacy; not for new compositions")
        for lane in self.lanes:
            tile = lane.tile.definition()
            if tile.kind != CoreKind.OPERATOR:
                raise CompositionError(f"lane tile {lane.tile.core!r} is {tile.kind.value}, not an operator")
            if tile.legacy:
                raise CompositionError(f"lane tile {lane.tile.core!r} is legacy (predates the stream+status contract); not for new compositions")
            if tile.count_port is None:
                raise CompositionError(f"lane tile {lane.tile.core!r} does not expose a status counter")
            if lane.partition is not None and self.partitioner is not None:
                raise CompositionError(f"lane tile {lane.tile.core!r} carries a partition filter but the scan already has a shared partitioner")
            if lane.partition is not None and lane.partition.definition().kind != CoreKind.OPERATOR:
                raise CompositionError(f"lane partition {lane.partition.core!r} is not an operator")

    def core_names(self) -> tuple[str, ...]:
        """Every registry core the composition instantiates, infrastructure
        included, deduplicated in first-use order. A shared partitioner
        replaces the broadcast."""
        names: dict[str, None] = {}
        for name in SCAN_INFRASTRUCTURE_CORES:
            if self.partitioner is not None and name == "stream-broadcast":
                continue
            names.setdefault(name)
        if self.partitioner is not None:
            names.setdefault(self.partitioner.core)
        for lane in self.lanes:
            if lane.partition is not None:
                names.setdefault(lane.partition.core)
            names.setdefault(lane.tile.core)
        return tuple(names)


def estimated_resources(spec: ScanCompositionSpec) -> ResourceEnvelope:
    """Sum of the measured envelopes of every instance the composition
    places (per-lane cores counted per lane). ``measured`` is the AND of
    the constituents — one unmeasured core marks the estimate."""
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
    if spec.partitioner is not None:
        add(spec.partitioner.core)
    else:
        add("stream-broadcast")
    for lane in spec.lanes:
        add("axi-record-writer")
        if lane.partition is not None:
            add(lane.partition.core)
        add(lane.tile.core)
    return ResourceEnvelope(lut=lut, ff=ff, bram36=bram, dsp=dsp, measured=measured)

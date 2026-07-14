from __future__ import annotations

import pytest

from dau_core.composition import CompositionError, LaneSpec, ScanCompositionSpec, TileSpec, estimated_resources
from dau_core.cores import core

_BAR = TileSpec(core="int32-bar-aggregation", config={"cfg_mode": "2'd0", "cfg_row_count": "64'd0"})


def _partition(lane: int, lanes: int) -> TileSpec:
    return TileSpec(core="int32-pair-key-filter", config={"cfg_key_mask": f"32'd{lanes - 1}", "cfg_key_match": f"32'd{lane}"})


def bar_noc_spec(lanes: int = 4) -> ScanCompositionSpec:
    return ScanCompositionSpec(
        name=f"bar-noc-{lanes}",
        lanes=tuple(LaneSpec(tile=_BAR, partition=_partition(lane, lanes)) for lane in range(lanes)),
    )


def test_spec_validates_lane_tiles() -> None:
    with pytest.raises(CompositionError, match="at least one lane"):
        ScanCompositionSpec(name="empty", lanes=())
    with pytest.raises(CompositionError, match="not an operator"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=TileSpec(core="stream-broadcast")),))
    with pytest.raises(CompositionError, match="status counter"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=TileSpec(core="int32-map-alu")),))
    with pytest.raises(CompositionError, match="legacy"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=TileSpec(core="int32-aggregation-tile")),))


def test_core_names_cover_infrastructure_and_lanes() -> None:
    names = bar_noc_spec().core_names()
    assert "axi-burst-reader" in names and "axi-write-mux" in names
    assert "int32-pair-key-filter" in names and "int32-bar-aggregation" in names
    assert len(names) == len(set(names))


def test_estimated_resources_scale_with_lanes() -> None:
    one = estimated_resources(bar_noc_spec(lanes=1))
    four = estimated_resources(bar_noc_spec(lanes=4))
    per_lane = core("axi-record-writer").resources.lut + core("int32-pair-key-filter").resources.lut + core("int32-bar-aggregation").resources.lut
    assert four.lut - one.lut == 3 * per_lane
    assert four.measured


def test_single_lane_filterless_scan_is_valid() -> None:
    spec = ScanCompositionSpec(name="bar-scan", lanes=(LaneSpec(tile=_BAR),))
    assert "int32-pair-key-filter" not in spec.core_names()
    assert estimated_resources(spec).lut > 0


def _range_partitioner(lanes: int) -> TileSpec:
    splitters = ", ".join(f"32'd{10 * (index + 1)}" for index in range(lanes - 1))
    return TileSpec(core="int32-range-partitioner", config={"cfg_splitters": f"{{{splitters}}}"})


def sorted_scan_spec(lanes: int = 4) -> ScanCompositionSpec:
    return ScanCompositionSpec(
        name=f"sorted-scan-{lanes}",
        lanes=tuple(LaneSpec(tile=TileSpec(core="int32-bitonic-sorter")) for _ in range(lanes)),
        partitioner=_range_partitioner(lanes),
    )


def test_shared_partitioner_replaces_broadcast() -> None:
    spec = sorted_scan_spec()
    names = spec.core_names()
    assert "int32-range-partitioner" in names
    assert "stream-broadcast" not in names
    assert "int32-bitonic-sorter" in names


def test_shared_partitioner_rejects_per_lane_partitions() -> None:
    with pytest.raises(CompositionError, match="already has a shared partitioner"):
        ScanCompositionSpec(
            name="bad",
            lanes=(LaneSpec(tile=_BAR, partition=_partition(0, 4)),),
            partitioner=_range_partitioner(4),
        )


def test_shared_partitioner_must_be_operator() -> None:
    with pytest.raises(CompositionError, match="not an operator"):
        ScanCompositionSpec(
            name="bad",
            lanes=(LaneSpec(tile=_BAR),),
            partitioner=TileSpec(core="stream-broadcast"),
        )


def test_sorted_scan_resources_use_partitioner_envelope() -> None:
    spec = sorted_scan_spec()
    envelope = estimated_resources(spec)
    assert envelope.measured
    partitioner = core("int32-range-partitioner").resources
    broadcast = core("stream-broadcast").resources
    assert partitioner is not None and broadcast is not None
    # the partitioner, not the broadcast, is in the sum
    without = estimated_resources(bar_noc_spec())
    assert envelope.lut != without.lut

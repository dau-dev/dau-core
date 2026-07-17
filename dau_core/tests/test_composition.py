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


def q6_scan_spec(lanes: int = 4) -> ScanCompositionSpec:
    """The q6 filter+reduce lane: key-partitioned fan-out, a chain of two
    row-predicate filters (the four-comparison WHERE split across two
    3-term tiles) and the revenue map, closed by the field-sum tile."""
    where_a = TileSpec(core="int32-row-predicate-filter", config={"cfg_op0": "3'd1", "cfg_constant0": "32'd568"})
    where_b = TileSpec(core="int32-row-predicate-filter", config={"cfg_op0": "3'd6", "cfg_constant0": "32'd5"})
    revenue = TileSpec(core="int32-row-map-alu", config={"cfg_op": "3'd4", "cfg_shift": "5'd15"})
    reduce_tile = TileSpec(core="int32-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"})
    return ScanCompositionSpec(
        name=f"q6-scan-{lanes}",
        lanes=tuple(LaneSpec(tile=reduce_tile, partition=_partition(lane, lanes), chain=(where_a, where_b, revenue)) for lane in range(lanes)),
    )


def test_chain_lanes_validate_and_close_core_names() -> None:
    spec = q6_scan_spec()
    names = spec.core_names()
    assert "int32-row-predicate-filter" in names
    assert "int32-row-map-alu" in names
    assert "int32-field-sum-aggregation" in names
    assert len(names) == len(set(names))


def test_chain_stages_must_be_non_legacy_operators() -> None:
    with pytest.raises(CompositionError, match="chain stage 'stream-broadcast' is stream-infra"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="stream-broadcast"),)),))
    with pytest.raises(CompositionError, match="chain stage 'int32-aggregation-tile' is legacy"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-aggregation-tile"),)),))


def test_chain_stages_need_no_status_counter() -> None:
    # int32-row-map-alu exposes no count port: legal mid-chain, illegal terminal
    spec = ScanCompositionSpec(name="ok", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-row-map-alu"),)),))
    assert "int32-row-map-alu" in spec.core_names()
    with pytest.raises(CompositionError, match="status counter"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=TileSpec(core="int32-row-map-alu")),))


def test_chain_resources_are_counted_per_lane() -> None:
    plain = estimated_resources(bar_noc_spec(lanes=2))
    chained = estimated_resources(
        ScanCompositionSpec(
            name="chained",
            lanes=tuple(LaneSpec(tile=_BAR, partition=_partition(lane, 2), chain=(TileSpec(core="int32-row-map-alu"),)) for lane in range(2)),
        )
    )
    stage = core("int32-row-map-alu").resources
    assert stage is not None
    assert chained.lut - plain.lut == 2 * stage.lut
    assert chained.dsp - plain.dsp == 2 * stage.dsp
    assert chained.measured


def test_exactly_one_success_close_out_per_lane_is_enforced() -> None:
    # a close-out-on-success tile mid-chain would leave a second pending
    # status after the writer consumed the first: rejected up front
    with pytest.raises(CompositionError, match="chain stage 'int32-keyed-predicate-filter' emits a success close-out"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-keyed-predicate-filter"),)),))
    # a silent-on-success terminal would leave the record writer waiting for
    # a close-out that never comes on any batch that forwards rows
    with pytest.raises(CompositionError, match="tile 'int32-row-predicate-filter' is silent on success"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=TileSpec(core="int32-row-predicate-filter")),))
    # the same one-close-out rule holds for the partition slot
    with pytest.raises(CompositionError, match="partition 'int32-keyed-predicate-filter' emits a success close-out"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, partition=TileSpec(core="int32-keyed-predicate-filter")),))
    # and the partition slot rejects legacy tiles like every other slot
    with pytest.raises(CompositionError, match="partition 'int32-aggregation-tile' is legacy"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, partition=TileSpec(core="int32-aggregation-tile")),))

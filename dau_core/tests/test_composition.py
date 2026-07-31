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


def test_tile_params_validate_declared_names_and_positive_values() -> None:
    # a declared parameter override on a chain-stage membership filter is accepted
    membership = TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": 6_000_000})
    spec = ScanCompositionSpec(name="q3-membership", lanes=(LaneSpec(tile=_BAR, chain=(membership,)),))
    assert spec.lanes[0].chain[0].params == {"KEY_SPACE": 6_000_000}

    # an undeclared parameter name is rejected
    with pytest.raises(CompositionError, match="no overridable parameter 'KEYSPACE'"):
        ScanCompositionSpec(
            name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-key-membership-filter", params={"KEYSPACE": 6_000_000}),)),)
        )
    # a core that declares no parameters rejects every override
    with pytest.raises(CompositionError, match="no overridable parameter 'KEY_SPACE' \\(declared: none\\)"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-row-map-alu", params={"KEY_SPACE": 8}),)),))
    # type garbage is rejected at the field boundary, never coerced (True -> 1
    # or "4" -> 4 would silently satisfy the positive-int contract)
    import pydantic

    for garbage in (True, "4", 4.0):
        with pytest.raises(pydantic.ValidationError):
            TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": garbage})
    # non-positive / non-int values are rejected
    with pytest.raises(CompositionError, match="must be a positive int"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": 0}),)),))
    with pytest.raises(CompositionError, match="must be a positive int"):
        ScanCompositionSpec(
            name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": -64}),)),)
        )


def test_minimum_constraint_mirrors_the_rolling_window_guard() -> None:
    # the rolling-moment window is >= 2 (a size-1 sample variance is undefined);
    # rejected at composition time, mirroring the tile's RTL elaboration guard
    ScanCompositionSpec(name="ok", lanes=(LaneSpec(tile=TileSpec(core="int32-rolling-moments", params={"WINDOW": 2})),))
    with pytest.raises(CompositionError, match="must be >= 2"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=TileSpec(core="int32-rolling-moments", params={"WINDOW": 1})),))


def test_maximum_constraint_mirrors_the_bucket_bits_guard() -> None:
    # BUCKET_BITS must be in [1, 31] (a chain-stage transform, over a bar lane);
    # the upper bound mirrors the tile's RTL elaboration guard
    bucket31 = TileSpec(core="int32-time-bucket-key", params={"BUCKET_BITS": 31})
    ScanCompositionSpec(name="ok", lanes=(LaneSpec(tile=_BAR, chain=(bucket31,)),))
    with pytest.raises(CompositionError, match="must be <= 31"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-time-bucket-key", params={"BUCKET_BITS": 32}),)),))


def test_choices_and_power_of_two_constraints_mirror_the_elaboration_guards() -> None:
    # the quad-stream widths are exactly 64 or 128 (the row-unpack OUT_WIDTH and
    # the dispatcher IN_WIDTH mirror their RTL elaboration guards), and
    # the dispatcher's mask decode needs a power-of-two NUM_PARTITIONS — all
    # rejected at composition time, never later as an HDL elaboration failure
    unpack_ok = TileSpec(core="int32-row-unpack", config=dict(_FRONT_UNPACK_CONFIG), params={"OUT_WIDTH": 128})
    ScanCompositionSpec(name="ok", lanes=(LaneSpec(tile=_BAR),), front_unpack=unpack_ok)
    with pytest.raises(CompositionError, match="must be one of 64, 128"):
        ScanCompositionSpec(
            name="bad",
            lanes=(LaneSpec(tile=_BAR),),
            front_unpack=TileSpec(core="int32-row-unpack", config=dict(_FRONT_UNPACK_CONFIG), params={"OUT_WIDTH": 192}),
        )
    dispatcher_ok = TileSpec(core="int32-key-mask-dispatcher", params={"IN_WIDTH": 128})
    ScanCompositionSpec(name="ok", lanes=tuple(LaneSpec(tile=_BAR) for _ in range(4)), partitioner=dispatcher_ok)
    with pytest.raises(CompositionError, match="must be one of 64, 128"):
        ScanCompositionSpec(
            name="bad",
            lanes=tuple(LaneSpec(tile=_BAR) for _ in range(4)),
            partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"IN_WIDTH": 192}),
        )
    with pytest.raises(CompositionError, match="must be a power of two"):
        ScanCompositionSpec(
            name="bad",
            lanes=tuple(LaneSpec(tile=_BAR) for _ in range(4)),
            partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 12}),
        )
    # the effective mask-mode partition count is the LANE count (the walker
    # derives NUM_PARTITIONS from it): three mask-mode lanes are rejected here,
    # not at Vivado elaboration
    with pytest.raises(CompositionError, match="must be a power of two in mask mode"):
        ScanCompositionSpec(
            name="bad-derived",
            lanes=tuple(LaneSpec(tile=_BAR) for _ in range(3)),
            partitioner=TileSpec(core="int32-key-mask-dispatcher"),
        )
    # the exact-branch mode lifts the power-of-two requirement (q19's three
    # disjunct branches) — and an explicit EXACT_BRANCH: 0 flag is a legal
    # enumerated choice, not a "non-positive" parameter
    ScanCompositionSpec(
        name="ok-exact",
        lanes=tuple(LaneSpec(tile=_BAR) for _ in range(3)),
        partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 3, "EXACT_BRANCH": 1}),
    )
    ScanCompositionSpec(
        name="ok-mask-explicit-flag",
        lanes=tuple(LaneSpec(tile=_BAR) for _ in range(4)),
        partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 4, "EXACT_BRANCH": 0}),
    )


def test_load_phase_shape_rules() -> None:
    # the two-phase load (v1): membership at chain[0] binding cfg_load to the
    # reserved load_phase symbol is accepted; every malformed shape is
    # rejected with a specific error
    def membership(cfg_load="load_phase", key_space=None, extra=None):
        config = {"cfg_load": cfg_load, "cfg_mode": "1'b0", "cfg_kind": "2'd1"}
        if extra:
            config.update(extra)
        params = {"KEY_SPACE": key_space} if key_space else {}
        return TileSpec(core="int32-key-membership-filter", config=config, params=params)

    # the q3 shape: one lane, membership first, KEY_SPACE % 128 == 0
    ok = ScanCompositionSpec(name="q3", lanes=(LaneSpec(tile=_BAR, chain=(membership(key_space=6_000_000),)),))
    assert "int32-key-membership-filter" in ok.core_names()

    # multi-lane broadcast: every lane loads its copy concurrently — legal
    ScanCompositionSpec(name="ok", lanes=tuple(LaneSpec(tile=_BAR, chain=(membership(),)) for _ in range(2)))

    # a baked cfg_load literal stays single-phase: none of the rules engage
    ScanCompositionSpec(
        name="ok",
        lanes=(
            LaneSpec(
                tile=_BAR,
                partition=TileSpec(core="int32-pair-key-filter", config={"cfg_key_mask": "32'd0", "cfg_key_match": "32'd0"}),
                chain=(membership(cfg_load="1'b0"),),
            ),
        ),
    )

    # the reserved symbol on any port but cfg_load is rejected
    with pytest.raises(CompositionError, match="only cfg_load may"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(membership(cfg_load="1'b0", extra={"cfg_mode": "load_phase"}),)),))
    # a load tile anywhere but chain[0] is rejected
    with pytest.raises(CompositionError, match="first chain stage"):
        ScanCompositionSpec(
            name="bad",
            lanes=(
                LaneSpec(
                    tile=_BAR,
                    chain=(
                        TileSpec(
                            core="int32-row-map-alu",
                            config={
                                "cfg_op": "3'd0",
                                "cfg_dst_field": "2'd1",
                                "cfg_src_a_field": "2'd1",
                                "cfg_src_b_field": "2'd2",
                                "cfg_shift": "5'd0",
                            },
                        ),
                        membership(),
                    ),
                ),
            ),
        )
    # non-uniform lanes: a loadless lane would misread the image
    with pytest.raises(CompositionError, match="EVERY lane"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(membership(),)), LaneSpec(tile=_BAR)))
    # a partition filter would filter the image
    with pytest.raises(CompositionError, match="partition filters"):
        ScanCompositionSpec(
            name="bad",
            lanes=(
                LaneSpec(
                    tile=_BAR,
                    partition=TileSpec(core="int32-pair-key-filter", config={"cfg_key_mask": "32'd0", "cfg_key_match": "32'd0"}),
                    chain=(membership(),),
                ),
            ),
        )
    # a front unpacker would re-slice the image words
    with pytest.raises(CompositionError, match="front unpacker"):
        ScanCompositionSpec(
            name="bad",
            lanes=(LaneSpec(tile=_BAR, chain=(membership(),)),),
            front_unpack=TileSpec(core="int32-row-unpack", config=dict(_FRONT_UNPACK_CONFIG)),
        )
    # the image must land on the 16-byte grid: KEY_SPACE % 128 != 0 rejected
    with pytest.raises(CompositionError, match="multiple of 128"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(membership(key_space=192),)),))
    # a shared partitioner would scatter the image across lanes
    with pytest.raises(CompositionError, match="shared partitioner"):
        ScanCompositionSpec(
            name="bad",
            lanes=tuple(LaneSpec(tile=_BAR, chain=(membership(),)) for _ in range(4)),
            partitioner=TileSpec(core="int32-range-partitioner", config={"cfg_splitters": "{32'h1, 32'h2, 32'h3}"}),
        )
    # load capability is registry truth: a core without a load_image kind
    # cannot be phase-bound even at chain[0]
    with pytest.raises(CompositionError, match="declares no load_image"):
        ScanCompositionSpec(
            name="bad",
            lanes=(
                LaneSpec(
                    tile=_BAR,
                    chain=(
                        TileSpec(
                            core="int32-row-map-alu",
                            config={
                                "cfg_load": "load_phase",
                                "cfg_op": "3'd0",
                                "cfg_dst_field": "2'd1",
                                "cfg_src_a_field": "2'd1",
                                "cfg_src_b_field": "2'd2",
                                "cfg_shift": "5'd0",
                            },
                        ),
                    ),
                ),
            ),
        )
    # the hash join's QUAD_ROWS image is 16-byte build rows — always on the
    # grid, so no KEY_SPACE % 128 rule (192 is legal here)
    ScanCompositionSpec(
        name="ok",
        lanes=(
            LaneSpec(
                tile=_BAR,
                chain=(
                    TileSpec(core="int32-broadcast-hash-join", config={"cfg_load": "load_phase", "cfg_out_sel": "2'd0"}, params={"KEY_SPACE": 192}),
                ),
            ),
        ),
    )


def test_param_override_is_validated_on_the_lane_tile_and_partition_slots() -> None:
    # the lane-tile slot validates its params
    with pytest.raises(CompositionError, match="lane tile 'int32-bar-aggregation' has no overridable parameter 'KEY_SPACE'"):
        ScanCompositionSpec(
            name="bad", lanes=(LaneSpec(tile=TileSpec(core="int32-bar-aggregation", config=dict(_BAR.config), params={"KEY_SPACE": 8})),)
        )
    # the partition slot validates its params too
    with pytest.raises(CompositionError, match="lane partition 'int32-pair-key-filter' has no overridable parameter 'KEY_SPACE'"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, partition=TileSpec(core="int32-pair-key-filter", params={"KEY_SPACE": 8})),))


def test_key_space_override_must_match_the_declared_multiple() -> None:
    # the membership bitmap is KEY_SPACE/64 words, so KEY_SPACE must be a positive multiple of 64
    for ok in (64, 128, 6_000_000):
        ScanCompositionSpec(name="ok", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": ok}),)),))
    with pytest.raises(CompositionError, match="must be a positive multiple of 64"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": 65}),)),))
    # the hash join's slot table is clog2(KEY_SPACE)-addressed, so any positive KEY_SPACE is valid
    ScanCompositionSpec(name="ok", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-broadcast-hash-join", params={"KEY_SPACE": 100}),)),))


def test_shared_partitioner_params_are_validated() -> None:
    # the shared-partitioner slot now validates its params (the slot previously bypassed validation)
    with pytest.raises(CompositionError, match="partitioner 'int32-key-membership-filter' has no overridable parameter 'TYPO'"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR),), partitioner=TileSpec(core="int32-key-membership-filter", params={"TYPO": 1}))
    with pytest.raises(CompositionError, match="partitioner 'int32-key-membership-filter' parameter 'KEY_SPACE' must be a positive multiple of 64"):
        ScanCompositionSpec(
            name="bad", lanes=(LaneSpec(tile=_BAR),), partitioner=TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": 65})
        )


def test_param_less_tiles_are_unchanged_and_price_at_default_size() -> None:
    # a param'd membership tile does not crash the resource estimate (the
    # envelope is looked up by core name, independent of KEY_SPACE)
    membership = TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": 6_000_000})
    spec = ScanCompositionSpec(name="q3", lanes=(LaneSpec(tile=_BAR, chain=(membership,)),))
    priced = estimated_resources(spec)
    # equals the default-size envelope sum: the param override does not (yet) rescale the estimate
    default_sized = estimated_resources(
        ScanCompositionSpec(name="q3-default", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-key-membership-filter"),)),))
    )
    assert priced.bram36 == default_sized.bram36
    assert priced.lut == default_sized.lut


_FRONT_UNPACK_CONFIG = {
    "cfg_field0_offset": "6'd0",
    "cfg_field0_width": "6'd32",
    "cfg_field0_signed": "1'b0",
    "cfg_field1_offset": "6'd32",
    "cfg_field1_width": "6'd27",
    "cfg_field1_signed": "1'b0",
    "cfg_field2_offset": "6'd0",
    "cfg_field2_width": "6'd1",
    "cfg_field2_signed": "1'b0",
    "cfg_field3_offset": "6'd0",
    "cfg_field3_width": "6'd1",
    "cfg_field3_signed": "1'b0",
    "cfg_bypass": "1'b0",
}


def _front_unpack() -> TileSpec:
    return TileSpec(core="int32-row-unpack", config=dict(_FRONT_UNPACK_CONFIG))


def test_front_unpack_validates_and_reaches_core_names_and_resources() -> None:
    # a broadcast-fan-out composition gains a front unpacker before the fan-out
    spec = ScanCompositionSpec(name="bar-noc-packed", lanes=bar_noc_spec().lanes, front_unpack=_front_unpack())
    assert "int32-row-unpack" in spec.core_names()
    # the front unpacker's envelope is added on top of the fan-out shape
    priced = estimated_resources(spec)
    without = estimated_resources(bar_noc_spec())
    unpack = core("int32-row-unpack").resources
    assert unpack is not None
    assert priced.lut - without.lut == unpack.lut
    assert priced.measured
    # it coexists with a shared partitioner too (still one clean-key row into the fan-out)
    partitioned = ScanCompositionSpec(
        name="sorted-scan-packed", lanes=sorted_scan_spec().lanes, partitioner=_range_partitioner(4), front_unpack=_front_unpack()
    )
    assert "int32-row-unpack" in partitioned.core_names()


def test_front_unpack_rejects_a_non_unpack_tile() -> None:
    with pytest.raises(CompositionError, match="is not the 'int32-row-unpack' feed transform"):
        ScanCompositionSpec(name="bad", lanes=bar_noc_spec().lanes, front_unpack=_BAR)
    with pytest.raises(CompositionError, match="is not the 'int32-row-unpack' feed transform"):
        ScanCompositionSpec(name="bad", lanes=bar_noc_spec().lanes, front_unpack=TileSpec(core="int32-pair-key-filter"))


def test_front_unpack_less_composition_is_unchanged() -> None:
    spec = bar_noc_spec()
    assert spec.front_unpack is None
    assert "int32-row-unpack" not in spec.core_names()
    assert estimated_resources(spec) == estimated_resources(bar_noc_spec())


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


def test_spec_canonical_round_trip_and_hash_stability() -> None:
    from dau_core.composition import canonical_spec_json, spec_from_canonical, spec_hash

    spec = bar_noc_spec()
    text = canonical_spec_json(spec)
    rebuilt = spec_from_canonical(text)
    assert rebuilt == spec
    assert spec_hash(rebuilt) == spec_hash(spec)
    assert canonical_spec_json(rebuilt) == text


def test_spec_hash_is_config_order_insensitive_and_content_sensitive() -> None:
    from dau_core.composition import spec_hash

    def build(config):
        return ScanCompositionSpec(name="t", lanes=(LaneSpec(tile=TileSpec(core="int32-bar-aggregation", config=config)),))

    a = build({"cfg_mode": "2'd0", "cfg_row_count": "64'd0"})
    b = build({"cfg_row_count": "64'd0", "cfg_mode": "2'd0"})  # same content, other insertion order
    c = build({"cfg_mode": "2'd1", "cfg_row_count": "64'd0"})  # different content
    assert spec_hash(a) == spec_hash(b)
    assert spec_hash(a) != spec_hash(c)


def test_spec_hash_pinned_fixture() -> None:
    """The design-cache key must be stable across releases: ANY change to
    the canonical form — field ordering, a new or changed spec field or
    default, hash construction — fails HERE, loudly, and must bump
    SPEC_CANONICAL_VERSION deliberately (defaults are included precisely so
    a changed default changes the key instead of colliding)."""
    from dau_core.composition import spec_hash

    spec = ScanCompositionSpec(
        name="pinned",
        lanes=(
            LaneSpec(
                tile=TileSpec(core="int32-bar-aggregation", config={"cfg_mode": "2'd0", "cfg_row_count": "64'd0"}),
                partition=TileSpec(core="int32-pair-key-filter", config={"cfg_key_mask": "32'd3", "cfg_key_match": "32'd1"}),
            ),
        ),
        burst_beats=16,
    )
    # canonical version 2: data_width joined the canonical form (the
    # width-tier knob) — every v1 cache key misses cold, by design
    assert spec_hash(spec) == "2:4b004e696dcc5278c65968c1560de4ff64d20b9426b08c35fd774177d79b8a75"


def test_spec_from_canonical_revalidates() -> None:
    """A tampered canonical form fails registry validation, never builds."""
    from dau_core.composition import canonical_spec_json, spec_from_canonical

    text = canonical_spec_json(bar_noc_spec())
    tampered = text.replace("int32-bar-aggregation", "int32-row-map-alu")  # silent-on-success: illegal terminal
    with pytest.raises(CompositionError):
        spec_from_canonical(tampered)


def test_spec_hash_stable_when_defaults_are_explicit() -> None:
    from dau_core.composition import spec_hash

    implicit = ScanCompositionSpec(name="t", lanes=(LaneSpec(tile=TileSpec(core="int32-bar-aggregation")),))
    explicit = ScanCompositionSpec(
        name="t",
        lanes=(LaneSpec(tile=TileSpec(core="int32-bar-aggregation", config={}, params={}), partition=None, chain=()),),
        burst_beats=32,
        addr_width=32,
        partitioner=None,
        front_unpack=None,
    )
    assert spec_hash(implicit) == spec_hash(explicit)


def test_wide_data_width_composition_rules() -> None:
    """The canonical spec mirrors the walker's width rules: a wide feed
    needs a dispatcher (broadcast is 64-bit only), wide packed reads are a
    typed follow-up, and the tier set is closed."""
    lane = LaneSpec(tile=TileSpec(core="int32-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"}))
    with pytest.raises(CompositionError, match="broadcast is 64-bit only"):
        ScanCompositionSpec(name="bad", lanes=(lane,), data_width=256)
    with pytest.raises(CompositionError, match="must be 64/128/256/512"):
        ScanCompositionSpec(name="bad", lanes=(lane,), data_width=96)
    wide = ScanCompositionSpec(
        name="wide",
        lanes=(lane,) * 4,
        partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"IN_WIDTH": 512}),
        data_width=512,
    )
    assert wide.data_width == 512
    # and it round-trips the canonical form
    from dau_core.composition import canonical_spec_json, spec_from_canonical

    assert spec_from_canonical(canonical_spec_json(wide)).data_width == 512

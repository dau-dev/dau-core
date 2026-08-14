from __future__ import annotations

import pytest

from dau_core.composition import CompositionError, LaneSpec, ScanCompositionSpec, TileSpec, estimated_resources, stream_shape_mismatches
from dau_core.registry import core

_BAR = TileSpec(core="int32-bar-aggregation", config={"cfg_mode": "2'd0", "cfg_row_count": "64'd0"})

# Pricing a measured tile names a board and a clock. These tests price at one
# coordinate every migrated tile carries a met point for; the Artix at 8 ns is
# deliberately NOT it, because two map-ALU tiles violate there and refusing to
# price them is the behaviour under test elsewhere.
_PART = "xc7k325tffg900-2"
_CLOCK_NS = 8.0


def _priced(spec: ScanCompositionSpec):
    return estimated_resources(spec, part=_PART, clock_ns=_CLOCK_NS)


def _tile_lut(name: str) -> int:
    """A tile's LUTs at the coordinate above.

    Every operator and stream-infra tile carries measurement points as of
    2026-08-11; the scalar branch remains for the one shell-adapter that
    has no clock to characterize against."""
    definition = core(name)
    if isinstance(definition.resources, (list, tuple)):
        return definition.resource_measurement(part=_PART, clock_ns=_CLOCK_NS, params={}).usable_envelope().lut
    return definition.resources.lut


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


def test_core_names_cover_infrastructure_and_lanes() -> None:
    names = bar_noc_spec().core_names()
    assert "axi-burst-reader" in names and "axi-write-mux" in names
    assert "int32-pair-key-filter" in names and "int32-bar-aggregation" in names
    assert len(names) == len(set(names))


def test_estimated_resources_scale_with_lanes() -> None:
    one = _priced(bar_noc_spec(lanes=1))
    four = _priced(bar_noc_spec(lanes=4))
    per_lane = _tile_lut("axi-record-writer") + _tile_lut("int32-pair-key-filter") + _tile_lut("int32-bar-aggregation")
    assert four.lut - one.lut == 3 * per_lane
    # not fully measured while the axi infra tiles still carry scalar
    # envelopes -- this flips to True when they migrate, which is the point
    assert not four.measured


def test_single_lane_filterless_scan_is_valid() -> None:
    spec = ScanCompositionSpec(name="bar-scan", lanes=(LaneSpec(tile=_BAR),))
    assert "int32-pair-key-filter" not in spec.core_names()
    assert _priced(spec).lut > 0


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
    envelope = _priced(spec)
    assert not envelope.measured  # the axi infra tiles are still scalar
    partitioner = core("int32-range-partitioner").resources
    broadcast = core("stream-broadcast").resources
    assert partitioner is not None and broadcast is not None
    # the partitioner, not the broadcast, is in the sum
    without = _priced(bar_noc_spec())
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


def test_chain_stages_must_be_operators() -> None:
    with pytest.raises(CompositionError, match="chain stage 'stream-broadcast' is stream-infra"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="stream-broadcast"),)),))


def test_chain_stages_need_no_status_counter() -> None:
    # int32-row-map-alu exposes no count port: legal mid-chain, illegal terminal
    spec = ScanCompositionSpec(name="ok", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-row-map-alu"),)),))
    assert "int32-row-map-alu" in spec.core_names()
    with pytest.raises(CompositionError, match="status counter"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=TileSpec(core="int32-row-map-alu")),))


def test_chain_resources_are_counted_per_lane() -> None:
    plain = _priced(bar_noc_spec(lanes=2))
    chained = _priced(
        ScanCompositionSpec(
            name="chained",
            lanes=tuple(LaneSpec(tile=_BAR, partition=_partition(lane, 2), chain=(TileSpec(core="int32-row-map-alu"),)) for lane in range(2)),
        )
    )
    stage = core("int32-row-map-alu").resource_measurement(part=_PART, clock_ns=_CLOCK_NS, params={}).usable_envelope()
    assert chained.lut - plain.lut == 2 * stage.lut
    assert chained.dsp - plain.dsp == 2 * stage.dsp
    assert not chained.measured  # the axi infra tiles are still scalar


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
    # would silently satisfy the positive-int contract)
    import pydantic

    for garbage in (True, 4.0):
        with pytest.raises(pydantic.ValidationError):
            TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": garbage})
    # a string is the ELEMENT TYPE spelling, and this parameter is a capacity,
    # not a type -- so "4" is refused by name rather than coerced to 4
    with pytest.raises(CompositionError, match="'KEY_SPACE' is not an element type"):
        TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": "4"})
    # non-positive / non-int values are rejected
    with pytest.raises(CompositionError, match="must be a positive int"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": 0}),)),))
    with pytest.raises(CompositionError, match="must be a positive int"):
        ScanCompositionSpec(
            name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": -64}),)),)
        )


def test_a_design_may_pick_an_element_type_by_name() -> None:
    """What the migration is FOR: a design says what its fields are, not how
    many bits they take."""
    typed = TileSpec(core="int32-row-predicate-filter", params={"FIELD_WIDTH": "int16"})
    ScanCompositionSpec(name="ok", lanes=(LaneSpec(tile=_BAR, chain=(typed,)),))
    # stored as the encoding, so everything downstream -- the `-generic` a
    # synthesis run passes, the measured coordinate it is priced against --
    # sees one integer form and never has to know how it was written
    assert typed.params == {"FIELD_WIDTH": 16}

    with pytest.raises(CompositionError, match="must be one of int16, int32, int64, got float32"):
        ScanCompositionSpec(
            name="bad", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-row-predicate-filter", params={"FIELD_WIDTH": "float32"}),)),)
        )
    with pytest.raises(CompositionError, match="names no element type"):
        TileSpec(core="int32-row-predicate-filter", params={"FIELD_WIDTH": "int32ish"})


def test_a_type_and_its_width_are_the_same_design() -> None:
    """int32 IS 32, so the two spellings are one cache key rather than two --
    a design cache that missed on spelling would rebuild an identical shell."""
    from dau_core.composition import spec_hash

    def spec(field: object) -> ScanCompositionSpec:
        return ScanCompositionSpec(
            name="q6-where",
            lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-row-predicate-filter", params={"FIELD_WIDTH": field}),)),),
        )

    assert spec_hash(spec("int32")) == spec_hash(spec(32))


def test_minimum_constraint_mirrors_the_rolling_window_guard() -> None:
    # the rolling-moment window is >= 2 (a size-1 sample variance is undefined);
    # rejected at composition time, mirroring the tile's RTL elaboration guard
    ScanCompositionSpec(name="ok", lanes=(LaneSpec(tile=TileSpec(core="int32-rolling-moments", params={"WINDOW": 2})),))
    with pytest.raises(CompositionError, match="must be >= 2"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=TileSpec(core="int32-rolling-moments", params={"WINDOW": 1})),))


def test_a_declared_zero_minimum_admits_zero() -> None:
    # the fixed-point tiles guard FRACTIONAL_BITS to [0, 31] in RTL, and Q0
    # (an integer floor divide) is the whole point of the lower bound: the
    # blanket positive-int rule must not make a declared minimum 0 unreachable
    ScanCompositionSpec(
        name="ok",
        lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-fixed-point-divide", params={"FRACTIONAL_BITS": 0}),)),),
    )
    with pytest.raises(CompositionError, match="must be >= 0"):
        ScanCompositionSpec(
            name="bad",
            lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-fixed-point-divide", params={"FRACTIONAL_BITS": -1}),)),),
        )


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
    # the wide unpack rides the matching-width dispatcher (broadcast is
    # 64-bit only, checked below)
    ScanCompositionSpec(
        name="ok",
        lanes=tuple(LaneSpec(tile=_BAR) for _ in range(4)),
        front_unpack=unpack_ok,
        partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"IN_WIDTH": 128}),
    )
    with pytest.raises(CompositionError, match="broadcast, which is 64-bit only"):
        ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=_BAR),), front_unpack=unpack_ok)
    with pytest.raises(CompositionError, match="must be one of 64, 128"):
        ScanCompositionSpec(
            name="bad",
            lanes=(LaneSpec(tile=_BAR),),
            front_unpack=TileSpec(core="int32-row-unpack", config=dict(_FRONT_UNPACK_CONFIG), params={"OUT_WIDTH": 192}),
        )
    dispatcher_ok = TileSpec(core="int32-key-mask-dispatcher", params={"IN_WIDTH": 128})
    # the widths must agree end to end: IN_WIDTH=128 pairs with the wide unpack
    ScanCompositionSpec(name="ok", lanes=tuple(LaneSpec(tile=_BAR) for _ in range(4)), partitioner=dispatcher_ok, front_unpack=unpack_ok)
    # 192 is now legal (the 3-word record bus in whole-record framing), so
    # the invalid case has to be a width no framing produces
    with pytest.raises(CompositionError, match="must be one of 64, 128, 192, 256, 512"):
        ScanCompositionSpec(
            name="bad",
            lanes=tuple(LaneSpec(tile=_BAR) for _ in range(4)),
            partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"IN_WIDTH": 96}),
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
    with pytest.raises(CompositionError, match="must be a power of two in MASK mode"):
        ScanCompositionSpec(
            name="bad-derived",
            lanes=tuple(LaneSpec(tile=_BAR) for _ in range(3)),
            partitioner=TileSpec(core="int32-key-mask-dispatcher"),
        )
    # the exact-branch mode lifts the power-of-two requirement (q19's three
    # disjunct branches) — and an explicit MODE: 0 is a legal enumerated
    # choice, not a "non-positive" parameter
    ScanCompositionSpec(
        name="ok-exact",
        lanes=tuple(LaneSpec(tile=_BAR) for _ in range(3)),
        partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 3, "MODE": 1}),
    )
    ScanCompositionSpec(
        name="ok-mask-explicit-flag",
        lanes=tuple(LaneSpec(tile=_BAR) for _ in range(4)),
        partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 4, "MODE": 0}),
    )
    # HASH keeps the power-of-two requirement: it shifts the product down to
    # the top log2(N) bits, so a non-power-of-two would index past the lanes
    ScanCompositionSpec(
        name="ok-hash",
        lanes=tuple(LaneSpec(tile=_BAR) for _ in range(4)),
        partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 4, "MODE": 2}),
    )
    with pytest.raises(CompositionError, match="must be a power of two in HASH mode"):
        ScanCompositionSpec(
            name="bad-hash",
            lanes=tuple(LaneSpec(tile=_BAR) for _ in range(3)),
            partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 3, "MODE": 2}),
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


def test_a_sized_tile_prices_at_its_size_not_its_default() -> None:
    """A parameter that changes the hardware has to change the price.

    This assertion used to be the OPPOSITE -- that a 6M-key membership
    filter priced identically to a default 4096-key one -- and it was true
    only because the tile carried a scalar envelope that answered every
    KEY_SPACE with the same numbers. The membership filter is a BITMAP, so
    its BRAM cost IS the key space: 1 BRAM36 at 4096, 192 at 6,000,000,
    which is 57% of the Artix's 335 budgeted blocks. Every composition
    using the silicon-proven configuration was priced at 1/192 of the BRAM
    it actually places.
    """
    membership = TileSpec(core="int32-key-membership-filter", params={"KEY_SPACE": 6_000_000})
    priced = _priced(ScanCompositionSpec(name="q3", lanes=(LaneSpec(tile=_BAR, chain=(membership,)),)))
    default_sized = _priced(
        ScanCompositionSpec(name="q3-default", lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-key-membership-filter"),)),))
    )
    assert priced.bram36 - default_sized.bram36 == 191.0, (priced.bram36, default_sized.bram36)
    assert priced.lut > default_sized.lut


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
    priced = _priced(spec)
    without = _priced(bar_noc_spec())
    assert priced.lut - without.lut == _tile_lut("int32-row-unpack")
    assert not priced.measured  # the axi infra tiles are still scalar
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
    assert _priced(spec) == _priced(bar_noc_spec())


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
    # canonical version 6: handle_table_capacity joined the canonical form (a
    # handle table is hardware) — every v5 cache key misses cold, by design
    assert spec_hash(spec) == "6:2ce10a25792ae816fa44d01b5568455392a19d3503cb5591a576b6b3ae387866"


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


def test_handle_table_capacity_is_design_identity_and_inert_at_zero() -> None:
    """A handle table is HARDWARE — a table of flops and a resolve stage in
    front of the reader — so a design that carries one must not share a cache
    key with one that does not, and eight slots is not sixteen. Zero is the
    no-table default: a spec that never mentions it keeps the shape it has
    today and round-trips unchanged."""
    from dau_core.composition import canonical_spec_json, spec_from_canonical, spec_hash

    lane = LaneSpec(tile=TileSpec(core="int32-bar-aggregation"))
    plain = ScanCompositionSpec(name="t", lanes=(lane,))
    tabled = ScanCompositionSpec(name="t", lanes=(lane,), handle_table_capacity=8)
    assert plain.handle_table_capacity == 0
    assert spec_hash(plain) != spec_hash(tabled)
    assert spec_hash(tabled) != spec_hash(ScanCompositionSpec(name="t", lanes=(lane,), handle_table_capacity=16))

    text = canonical_spec_json(plain)
    assert '"handle_table_capacity":0' in text  # defaults are included, which is what makes the miss a miss and not a collision
    rebuilt = spec_from_canonical(text)
    assert rebuilt == plain
    assert rebuilt.handle_table_capacity == 0
    assert canonical_spec_json(spec_from_canonical(canonical_spec_json(tabled))) == canonical_spec_json(tabled)

    with pytest.raises(CompositionError, match="handle_table_capacity must be 0"):
        ScanCompositionSpec(name="t", lanes=(lane,), handle_table_capacity=-1)


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


def test_throughput_reproduces_the_measured_rate_ladder():
    """The rate model recomputes silicon: 64-bit two-beat = 1/2 row/cycle
    (~99 ms q6), the 512-bit dispatch feed = 1 (the serializer, 60.3 ms
    measured 2026-07-30), and MORE LANES DO NOT HELP past the dispatcher's
    declared ingest — the motivation for the wide-sum lane / distributor."""
    from fractions import Fraction

    from dau_core.composition import LaneSpec, ScanCompositionSpec, TileSpec, throughput_rows_per_cycle

    lane = LaneSpec(tile=TileSpec(core="int32-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"}))
    dispatcher = TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 4})
    wide_dispatcher = TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 4, "IN_WIDTH": 512})

    assert throughput_rows_per_cycle(ScanCompositionSpec(name="t", lanes=(lane,) * 4)) == Fraction(1, 2)
    # the dispatcher's own 64-bit two-beat framing halves it before the lanes
    assert throughput_rows_per_cycle(ScanCompositionSpec(name="t", lanes=(lane,) * 4, partitioner=dispatcher, data_width=64)) == Fraction(1, 2)
    assert throughput_rows_per_cycle(ScanCompositionSpec(name="t", lanes=(lane,) * 4, partitioner=wide_dispatcher, data_width=512)) == Fraction(1)
    # dispatcher-capped (the serializer): 8 lanes buy nothing until multi-row lands
    assert throughput_rows_per_cycle(ScanCompositionSpec(name="t", lanes=(lane,) * 8, partitioner=wide_dispatcher, data_width=512)) == Fraction(1)
    # two lanes still meet the serializer's one row per cycle
    assert throughput_rows_per_cycle(ScanCompositionSpec(name="t", lanes=(lane,) * 2, partitioner=wide_dispatcher, data_width=512)) == Fraction(1)
    # the width-coupling gate: a wide feed into a 64-bit-framed dispatcher
    # is a miswiring, refused at construction
    import pytest as _pytest

    from dau_core.composition import CompositionError

    with _pytest.raises(CompositionError, match="does not match the 512-bit feed framing"):
        ScanCompositionSpec(name="t", lanes=(lane,) * 4, partitioner=dispatcher, data_width=512)


def test_throughput_packed_front_re_times_delivery():
    from fractions import Fraction

    from dau_core.composition import LaneSpec, ScanCompositionSpec, TileSpec, throughput_rows_per_cycle

    lane = LaneSpec(tile=TileSpec(core="int32-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"}))
    dispatcher = TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 4})
    wide_dispatcher = TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 4, "IN_WIDTH": 128})
    narrow = TileSpec(core="int32-row-unpack")
    wide = TileSpec(core="int32-row-unpack", params={"OUT_WIDTH": 128})

    assert throughput_rows_per_cycle(ScanCompositionSpec(name="t", lanes=(lane,) * 4, partitioner=dispatcher, front_unpack=narrow)) == Fraction(1, 2)
    assert throughput_rows_per_cycle(ScanCompositionSpec(name="t", lanes=(lane,) * 4, partitioner=wide_dispatcher, front_unpack=wide)) == Fraction(1)


def test_the_wide_lane_shape_composes_and_rate_matches():
    """The wide lane: one lane of wide-row-stream stages consuming reader
    beats directly — SLOTS rows per cycle, no partitioner. The q6 shape at
    512 rates 4 rows/cycle (the parallel-row target)."""
    from fractions import Fraction

    from dau_core.composition import throughput_rows_per_cycle

    wide_sum = TileSpec(core="int32-wide-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"})
    wide_pred = TileSpec(core="int32-wide-row-predicate-filter", config={})
    wide_map = TileSpec(core="int32-wide-row-map-alu", config={})

    spec = ScanCompositionSpec(name="w", lanes=(LaneSpec(tile=wide_sum, chain=(wide_pred, wide_map)),), data_width=512)
    assert throughput_rows_per_cycle(spec) == Fraction(4)
    # no broadcast is placed: source closure and pricing must not count one
    assert "stream-broadcast" not in spec.core_names()

    # the wide TILES carry measured points at this coordinate, so pricing
    # resolves rather than raising. The composite `measured` flag is False
    # because the scan infrastructure cores are still on scalar envelopes,
    # which cannot answer for a specific part -- that is the flag doing its
    # job, not a failure to price.
    priced = _priced(spec)
    assert priced.lut > 0
    # SLOTS=1 gives the 128-bit tier a full-rate lane with no dispatcher
    narrow = ScanCompositionSpec(
        name="w1",
        lanes=(LaneSpec(tile=TileSpec(core="int32-wide-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"}, params={"SLOTS": 1})),),
        data_width=128,
    )
    assert throughput_rows_per_cycle(narrow) == Fraction(1)


def test_wide_combinational_tile_pipeline_depth_bounds_are_enforced():
    wide_sum = TileSpec(core="int32-wide-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"})

    for core_name in ("int32-wide-row-predicate-filter", "int32-wide-row-map-alu"):
        for depth in (1, 8):
            stage = TileSpec(core=core_name, config={}, params={"PIPELINE_DEPTH": depth})
            ScanCompositionSpec(name="ok", lanes=(LaneSpec(tile=wide_sum, chain=(stage,)),), data_width=512)
        with pytest.raises(CompositionError, match="must be a positive int"):
            stage = TileSpec(core=core_name, config={}, params={"PIPELINE_DEPTH": 0})
            ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=wide_sum, chain=(stage,)),), data_width=512)
        with pytest.raises(CompositionError, match="must be <= 8"):
            stage = TileSpec(core=core_name, config={}, params={"PIPELINE_DEPTH": 9})
            ScanCompositionSpec(name="bad", lanes=(LaneSpec(tile=wide_sum, chain=(stage,)),), data_width=512)


def test_the_wide_lane_shape_rules_are_enforced():
    wide_sum = TileSpec(core="int32-wide-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"})
    wide_lane = LaneSpec(tile=wide_sum)
    # data_width must equal SLOTS x 128
    with pytest.raises(CompositionError, match="SLOTS x 128"):
        ScanCompositionSpec(name="bad", lanes=(wide_lane,))
    # exactly one lane
    with pytest.raises(CompositionError, match="exactly one lane"):
        ScanCompositionSpec(name="bad", lanes=(wide_lane, wide_lane), data_width=512)
    # no partitioner
    with pytest.raises(CompositionError, match="cannot feed it"):
        ScanCompositionSpec(
            name="bad",
            lanes=(wide_lane,),
            partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"IN_WIDTH": 512}),
            data_width=512,
        )
    # every stage wide: a 64-bit chain stage cannot wire into the wide beat
    with pytest.raises(CompositionError, match="mixed framings cannot wire"):
        ScanCompositionSpec(
            name="bad",
            lanes=(LaneSpec(tile=wide_sum, chain=(TileSpec(core="int32-row-map-alu", config={}),)),),
            data_width=512,
        )
    # SLOTS must agree across the lane
    with pytest.raises(CompositionError, match="SLOTS disagree"):
        ScanCompositionSpec(
            name="bad",
            lanes=(LaneSpec(tile=wide_sum, chain=(TileSpec(core="int32-wide-row-map-alu", config={}, params={"SLOTS": 2}),)),),
            data_width=512,
        )
    # a wide stage inside a NARROW lane is still a mixed framing
    with pytest.raises(CompositionError, match="wide-row-stream"):
        ScanCompositionSpec(
            name="bad",
            lanes=(LaneSpec(tile=_BAR, chain=(TileSpec(core="int32-wide-row-map-alu", config={}),)),),
        )


def test_wide_row_stream_cores_are_refused_in_the_partitioner_slot() -> None:
    with pytest.raises(CompositionError, match="not a wide chain stage"):
        ScanCompositionSpec(
            name="bad",
            lanes=tuple(LaneSpec(tile=_BAR) for _ in range(4)),
            partitioner=TileSpec(core="int32-wide-row-predicate-filter", config={}),
        )


def test_the_fused_chain_shape_is_declared_not_inferred() -> None:
    """A terminal-shaped tile mid-lane (the temporal hero's as-of join and
    grouped aggregator) composes only when the spec DECLARES the fused
    chain: the refusal still catches an aggregator dropped into a chain by
    accident, while the declaration says the caller accepts that mid-stage
    close-outs are drained and only their errors reach the lane status."""
    lane = LaneSpec(
        tile=TileSpec(core="int32-rolling-moments", params={"WINDOW": 3}),
        chain=(
            TileSpec(core="int32-asof-backward"),
            TileSpec(core="int32-time-bucket-key"),
            TileSpec(core="int32-grouped-field-aggregation"),
        ),
    )
    with pytest.raises(CompositionError, match="fused_chain=True"):
        ScanCompositionSpec(name="hero", lanes=(lane,))

    spec = ScanCompositionSpec(name="hero", lanes=(lane,), fused_chain=True)
    assert spec.fused_chain is True
    # the flag is part of the design's identity: it changes the emitted
    # status glue, so two otherwise-identical specs must not share a key
    from dau_core.composition import spec_hash

    plain_lane = LaneSpec(tile=TileSpec(core="int32-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"}))
    a = ScanCompositionSpec(name="p", lanes=(plain_lane,))
    b = ScanCompositionSpec(name="p", lanes=(plain_lane,), fused_chain=True)
    assert spec_hash(a) != spec_hash(b)


def _temporal_hero_spec(*, include_feature_gather: bool = True) -> ScanCompositionSpec:
    stages = [
        TileSpec(core="int32-asof-backward"),
        TileSpec(core="int32-market-feature"),
    ]
    if include_feature_gather:
        stages.append(TileSpec(core="int32-record-gather", params={"IN_WORDS": 7, "OUT_WORDS": 3}))
    stages.extend(
        (
            TileSpec(core="int32-time-bucket-key"),
            TileSpec(core="int32-grouped-field-aggregation"),
            TileSpec(core="int32-record-gather", params={"IN_WORDS": 11, "OUT_WORDS": 1}),
        )
    )
    return ScanCompositionSpec(
        name="temporal-hero",
        lanes=(LaneSpec(tile=TileSpec(core="int32-rolling-moments"), chain=tuple(stages)),),
        fused_chain=True,
    )


def test_temporal_hero_gathers_reconcile_every_stream_shape() -> None:
    assert stream_shape_mismatches(_temporal_hero_spec()) == ()


def test_stream_shape_mismatch_names_both_cores_and_shapes() -> None:
    assert stream_shape_mismatches(_temporal_hero_spec(include_feature_gather=False)) == (
        ("lane 0: producer 'int32-market-feature' emits 7-word records, but consumer 'int32-time-bucket-key' expects 3-word records"),
    )


def test_stream_shape_mismatches_include_front_feed_and_partitioner() -> None:
    spec = ScanCompositionSpec(
        name="front-mismatch",
        lanes=(LaneSpec(tile=TileSpec(core="int32-asof-backward")),),
        front_unpack=TileSpec(core="int32-row-unpack"),
        partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"RECORD_WORDS": 3}),
    )
    assert stream_shape_mismatches(spec) == (
        ("front feed: producer 'int32-row-unpack' emits 2-word records, but consumer 'int32-key-mask-dispatcher' expects 3-word records"),
    )


def test_selectivity_reconciles_the_model_with_q6_silicon() -> None:
    """The rate benches measured the q6 map stage at one row per SEVEN
    cycles standalone, which made the no-selectivity model predict 1/7
    for q6 — 3.4x below the 0.483 rows/cycle silicon actually ran. Both
    numbers are right: two predicates upstream pass ~2% of rows, so the
    map never binds the feed. Charging each stage against the rows that
    reach it restores agreement, and the worst case stays the default
    because selectivity is data-dependent."""
    from fractions import Fraction

    from dau_core.composition import throughput_rows_per_cycle

    predicate = {
        "cfg_field0": "2'd0",
        "cfg_op0": "3'd6",
        "cfg_constant0": "32'd0",
        "cfg_combine01": "2'd0",
        "cfg_field1": "2'd0",
        "cfg_op1": "3'd0",
        "cfg_constant1": "32'd0",
        "cfg_combine2": "2'd0",
        "cfg_field2": "2'd0",
        "cfg_op2": "3'd0",
        "cfg_constant2": "32'd0",
    }
    row_map = {
        "cfg_op": "3'd4",
        "cfg_dst_field": "2'd1",
        "cfg_src_a_field": "2'd1",
        "cfg_src_b_field": "2'd2",
        "cfg_shift": "5'd15",
    }
    lane = LaneSpec(
        tile=TileSpec(core="int32-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"}),
        chain=(
            TileSpec(core="int32-row-predicate-filter", config=predicate),
            TileSpec(core="int32-row-predicate-filter", config=predicate),
            TileSpec(core="int32-row-map-alu", config=row_map),
        ),
    )
    q6 = ScanCompositionSpec(name="q6-scan", lanes=(lane,) * 4)

    # default: every stage charged as if every row reached it
    assert throughput_rows_per_cycle(q6) == Fraction(1, 7)

    # q6's measured funnel: 6,001,215 rows in, 114,160 out. One entry per
    # chain stage plus the terminal (what LEAVES the last stage).
    passes = (Fraction(1), Fraction(1, 5), Fraction(114160, 6001215), Fraction(114160, 6001215))
    with_selectivity = throughput_rows_per_cycle(q6, stage_pass_fractions=dict.fromkeys(range(4), passes))
    assert with_selectivity == Fraction(1, 2)
    silicon = Fraction(6001215, 99) / Fraction(125_000)  # 6.0M rows / 99.3 ms at 125 MHz
    assert abs(float(with_selectivity) - float(silicon)) / float(silicon) < 0.05

    with pytest.raises(CompositionError, match="pass fractions"):
        throughput_rows_per_cycle(q6, stage_pass_fractions={0: (Fraction(1),)})
    with pytest.raises(CompositionError, match=r"\[0, 1\]"):
        throughput_rows_per_cycle(q6, stage_pass_fractions=dict.fromkeys(range(4), (Fraction(2),) * 4))

    # a stage nothing reaches cannot bind the feed (and must not divide by zero)
    starved = dict.fromkeys(range(4), (Fraction(1), Fraction(0), Fraction(0), Fraction(0)))
    assert throughput_rows_per_cycle(q6, stage_pass_fractions=starved) == Fraction(1, 2)

    # under a shared partitioner the lane rates are SUMMED, which key routing
    # cannot honour when lanes differ: refuse rather than overpredict
    dispatched = ScanCompositionSpec(
        name="q6-dispatch",
        lanes=(lane,) * 4,
        partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 4}),
    )
    uneven = {0: passes, 1: (Fraction(1), Fraction(1), Fraction(1), Fraction(1)), 2: passes, 3: passes}
    with pytest.raises(CompositionError, match="identical across partitioned lanes"):
        throughput_rows_per_cycle(dispatched, stage_pass_fractions=uneven)


def _record_dispatcher(record_words: int = 3) -> TileSpec:
    """The dispatcher as it sits BEHIND a gearbox: its bus is one whole
    record wide, so it takes RECORD_INPUT rather than a row framing."""
    return TileSpec(
        core="int32-key-mask-dispatcher",
        params={"RECORD_INPUT": 1, "RECORD_WORDS": record_words, "IN_WIDTH": record_words * 64, "NUM_PARTITIONS": 4},
    )


def _front_gearbox(record_words: int = 3) -> TileSpec:
    return TileSpec(core="record-gearbox", params={"IN_WIDTH": 512, "RECORD_WORDS": record_words})


def test_front_gearbox_reaches_core_names_and_resources() -> None:
    """The gearbox must appear in core_names: that tuple is what stages the
    HDL a build compiles, so a gearbox missing from it is instantiated by
    the generated top and never handed to the toolchain — the design
    black-boxes at synthesis."""
    spec = ScanCompositionSpec(
        name="geared-scan", lanes=sorted_scan_spec().lanes, data_width=512, partitioner=_record_dispatcher(), front_gearbox=_front_gearbox()
    )
    assert "record-gearbox" in spec.core_names()
    priced = _priced(spec)
    without = _priced(ScanCompositionSpec(name="geared-scan", lanes=sorted_scan_spec().lanes, data_width=512, partitioner=_record_dispatcher()))
    assert priced.lut - without.lut == _tile_lut("record-gearbox")


def test_a_geared_design_does_not_share_a_hash_with_an_ungeared_one() -> None:
    """The spec hash keys the design cache. If the gearbox were invisible to
    the canonical form, a cached ungeared bitstream would be handed back for
    a geared design and silently run the wrong front."""
    from dau_core.composition import spec_hash

    lanes = sorted_scan_spec().lanes
    geared = ScanCompositionSpec(name="scan", lanes=lanes, data_width=512, partitioner=_record_dispatcher(), front_gearbox=_front_gearbox())
    plain = ScanCompositionSpec(name="scan", lanes=lanes, data_width=512, partitioner=_record_dispatcher())
    assert spec_hash(geared) != spec_hash(plain)
    # and the record size is part of that identity: a 3-word and a 4-word
    # gearbox are different hardware
    assert spec_hash(geared) != spec_hash(
        ScanCompositionSpec(name="scan", lanes=lanes, data_width=512, partitioner=_record_dispatcher(4), front_gearbox=_front_gearbox(4))
    )


def test_front_gearbox_refuses_a_narrow_feed_and_a_rival_front() -> None:
    lanes = bar_noc_spec().lanes
    with pytest.raises(CompositionError, match="requires data_width > 64"):
        ScanCompositionSpec(name="scan", lanes=lanes, front_gearbox=_front_gearbox())
    with pytest.raises(CompositionError, match="is not the 'record-gearbox' reframing core"):
        ScanCompositionSpec(
            name="scan",
            lanes=lanes,
            data_width=512,
            partitioner=_record_dispatcher(),
            front_gearbox=TileSpec(core="int32-row-unpack", config=dict(_FRONT_UNPACK_CONFIG)),
        )


def _stream_job_spec(job: str = "bram-stream-job", tile: str = "int32-bar-aggregation") -> ScanCompositionSpec:
    return ScanCompositionSpec(name="stream", stream_job=job, lanes=(LaneSpec(tile=TileSpec(core=tile)),))


def test_a_stream_job_composition_declares_its_own_infrastructure() -> None:
    """A stream job stages one tile chain through its job core instead of
    reading bursts and fanning out. Until it could be declared, its cores
    reached the build as a hand-copied list — so nothing checked that the
    list still matched what the top instantiates."""
    spec = _stream_job_spec()
    assert spec.core_names() == ("bram-stream-job", "identity-registers", "int32-bar-aggregation")
    assert _stream_job_spec("axi-stream-job").core_names()[0] == "axi-stream-job"
    # the scan shell's own infrastructure must not leak in
    assert "axi-burst-reader" not in spec.core_names()
    assert "stream-broadcast" not in spec.core_names()

    # and it is priced: a design that is not priced is a design whose LUT
    # budget is a guess
    priced = _priced(spec)
    assert priced.lut > 0


def test_a_stream_job_composition_refuses_scan_shaped_fields() -> None:
    """The stream job has no fan-out and no burst feed, so accepting a
    partitioner or a front stage would let a spec describe hardware the
    shell cannot build."""
    lanes = (LaneSpec(tile=TileSpec(core="int32-bar-aggregation")),)
    with pytest.raises(CompositionError, match="no fan-out or front stage"):
        ScanCompositionSpec(name="s", stream_job="bram-stream-job", lanes=lanes, partitioner=_record_dispatcher())
    with pytest.raises(CompositionError, match="exactly one lane"):
        ScanCompositionSpec(name="s", stream_job="bram-stream-job", lanes=lanes * 2)
    with pytest.raises(CompositionError, match="stream_job must be one of"):
        ScanCompositionSpec(name="s", stream_job="not-a-job", lanes=lanes)
    # the handle table resolves an address for a burst reader the stream job
    # does not have, and its top decodes no handle aperture — a capacity here
    # would be silently dropped, which is the raw-address exposure the table
    # exists to close
    with pytest.raises(CompositionError, match="no burst reader to resolve a handle for"):
        ScanCompositionSpec(name="s", stream_job="bram-stream-job", lanes=lanes, handle_table_capacity=4)


def test_the_scan_shell_still_requires_a_lane_counter() -> None:
    """The count_port waiver is the STREAM job's, not a general relaxation:
    the scan shell's record writer sizes its write-out from that port."""
    countless = (LaneSpec(tile=TileSpec(core="int32-record-batch-aggregation")),)
    # fine in a stream job, which stages records through its job core
    assert ScanCompositionSpec(name="s", stream_job="bram-stream-job", lanes=countless).core_names()
    with pytest.raises(CompositionError, match="does not expose a status counter"):
        ScanCompositionSpec(name="s", lanes=countless)


def test_addr_width_is_32_or_64_only() -> None:
    """A job master of some intermediate width is not hardware anyone builds,
    and permitting it would mean carrying padding logic through every
    register slice for a shape with nothing behind it: the AXI-Lite read
    path zero-extends cleanly at exactly 32 and 64 and needs an explicit
    pad at anything else."""
    lanes = bar_noc_spec().lanes
    for width in (32, 64):
        assert ScanCompositionSpec(name="scan", lanes=lanes, addr_width=width).addr_width == width
    for width in (16, 40, 48, 63, 128):
        with pytest.raises(CompositionError, match="addr_width must be 32 or 64"):
            ScanCompositionSpec(name="scan", lanes=lanes, addr_width=width)


def test_a_packed_front_can_feed_a_wide_lane() -> None:
    """The packed feed and the wide lane compose.

    Each is silicon-proven alone and they were mutually exclusive: the wide
    lane read whole quad rows straight from the reader, so it burned 16
    bytes of DDR per row, and the next SLOTS doubling would have demanded
    19.2 GB/s from a 12.8 GB/s DDR3. Packed, the same lane demands 9.6.

    Two widths are in play and they are NOT the same number. ``data_width``
    is the READ width -- SLOTS packed 64-bit rows per beat -- while the lane
    downstream of the unpacker sees SLOTS whole 128-bit quad rows. Note the
    consequence: an unpacked SLOTS=8 lane would need data_width=1024, which
    is not a legal tier, so packing is what puts eight slots in reach at all.
    """
    chain = (TileSpec(core="int32-wide-row-predicate-filter", params={"SLOTS": 8}),)
    lane = LaneSpec(
        tile=TileSpec(core="int32-wide-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"}, params={"SLOTS": 8}),
        chain=chain,
    )
    front = TileSpec(core="int32-row-unpack", params={"OUT_WIDTH": 1024})
    spec = ScanCompositionSpec(name="packed-wide", lanes=(lane,), front_unpack=front, data_width=512)
    assert spec.data_width == 512  # the PACKED read: 8 rows x 64 bits

    # the front must emit exactly what the lane consumes
    with pytest.raises(CompositionError, match="emits 512-bit beats"):
        ScanCompositionSpec(
            name="mismatch",
            lanes=(lane,),
            front_unpack=TileSpec(core="int32-row-unpack", params={"OUT_WIDTH": 512}),
            data_width=512,
        )
    # and data_width still describes the packed read, not the quad-row side
    with pytest.raises(CompositionError, match="reads SLOTS packed 64-bit rows"):
        ScanCompositionSpec(name="bad-read", lanes=(lane,), front_unpack=front, data_width=256)


def test_a_packed_wide_feed_rates_at_the_unpacker_not_at_one_row() -> None:
    """The packed feed's ceiling is the READER's packed-row supply, which is
    data_width/64, not a flat one row per cycle.

    The flat cap read the packed path as "one 8-byte row per 64-bit beat",
    true only while data_width was 64. A packed WIDE feed hands the unpacker
    data_width/64 packed rows per beat, and capping at 1 under-reported the
    silicon-measured eight-slot lane by 8x -- the design would have been
    priced as no faster than the build it replaced.
    """
    from fractions import Fraction

    from dau_core.composition import LaneSpec, ScanCompositionSpec, TileSpec, throughput_rows_per_cycle

    chain = (TileSpec(core="int32-wide-row-predicate-filter", params={"SLOTS": 8}),)
    lane = LaneSpec(
        tile=TileSpec(core="int32-wide-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"}, params={"SLOTS": 8}),
        chain=chain,
    )
    spec = ScanCompositionSpec(
        name="packed-wide",
        lanes=(lane,),
        front_unpack=TileSpec(core="int32-row-unpack", params={"OUT_WIDTH": 1024}),
        data_width=512,
    )
    assert throughput_rows_per_cycle(spec) == Fraction(8)

    # the classic 64-bit packed path is unchanged: one packed row per beat
    narrow_lane = LaneSpec(tile=TileSpec(core="int32-field-sum-aggregation", config={"cfg_field_mask": "4'b0010"}))
    narrow = ScanCompositionSpec(
        name="packed-narrow",
        lanes=(narrow_lane,) * 4,
        partitioner=TileSpec(core="int32-key-mask-dispatcher", params={"NUM_PARTITIONS": 4}),
        front_unpack=TileSpec(core="int32-row-unpack"),
    )
    assert throughput_rows_per_cycle(narrow) == Fraction(1, 2)

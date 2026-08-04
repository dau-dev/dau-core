from __future__ import annotations

import pytest

from dau_core.operators import (
    KEYED_STATE_MAX_KEYS,
    SCRATCHPAD_MAX_WORDS,
    TOP_K_MAX,
    BarAggregate,
    BroadcastJoinConfig,
    FieldSumAggregate,
    FilterPredicate,
    GroupAggregate,
    GroupedFieldSumAggregate,
    JoinSource,
    KeyedStateErrorCode,
    KeyedStateOp,
    MapConfig,
    MapOp,
    MembershipBuildSet,
    MembershipKind,
    MembershipMode,
    PredicateCombine,
    PredicateOp,
    RowField,
    RowMapConfig,
    RowPredicateConfig,
    RowPredicateTerm,
    ScratchpadErrorCode,
    TopKErrorCode,
    UnpackFieldSpec,
    bar_aggregate_rows,
    broadcast_hash_join_rows,
    field_sum_rows,
    filter_rows,
    filter_rows_by_membership,
    filter_rows_by_predicate,
    filter_values,
    gather_records,
    grouped_aggregate_rows,
    grouped_field_sum_rows,
    keyed_state_scan,
    map_row_fields,
    map_rows,
    map_values,
    mask_dispatch_rows,
    pack_splitters,
    scratchpad_scan,
    streaming_top_k,
    stride_distribute_rows,
    unpack_rows,
)

_VALUES = [7, -2, 5, 11, 5, 0, -100, 2**31 - 1, -(2**31)]


def _gather_selector(word: int, *, high: bool = False, shift: int = 0) -> int:
    return word | (int(high) << 6) | (shift << 7)


def test_record_gather_reproduces_feature_to_bucket_projection() -> None:
    time = 0x1122_3344_5566_7788
    price_symbol = 0x89AB_CDEF_0123_4567
    size = 0x8000_0002
    signed_volume = 0xFFFF_FFFA
    feature = [time, price_symbol, 0, size, 0, signed_volume, 0]
    selectors = (
        _gather_selector(1),
        _gather_selector(1, high=True),
        _gather_selector(0),
        _gather_selector(0, high=True),
        _gather_selector(3),
        _gather_selector(5),
    )

    assert gather_records(feature, in_words=7, out_words=3, selectors=selectors) == [
        price_symbol,
        time,
        (signed_volume << 32) | size,
    ]


def test_record_gather_reproduces_bar_to_roll_logical_key_extract() -> None:
    bucket_bits = 16
    key = 0xF234_5678
    close = 0x89AB_CDEF
    bar = [key, (close << 32) | 0x1234_5678, *([0] * 9)]
    selectors = (
        _gather_selector(0, shift=bucket_bits),
        _gather_selector(1, high=True),
    )

    assert gather_records(bar, in_words=11, out_words=1, selectors=selectors) == [(close << 32) | 0x0000_F234]


def test_record_gather_preserves_record_order() -> None:
    selectors = (_gather_selector(1), _gather_selector(0, high=True))
    words = [0xAAAA_AAAA_0000_0001, 0xBBBB_BBBB_0000_0002, 0xCCCC_CCCC_0000_0003, 0xDDDD_DDDD_0000_0004]
    assert gather_records(words, in_words=2, out_words=1, selectors=selectors) == [
        0xAAAA_AAAA_0000_0002,
        0xCCCC_CCCC_0000_0004,
    ]


@pytest.mark.parametrize(
    ("words", "in_words", "out_words", "selectors"),
    [
        ([1], 2, 1, (0, 0)),
        ([1], 1, 1, (0,)),
        ([1], 1, 1, (1, 0)),
        ([1], 1, 1, (1 << 12, 0)),
    ],
)
def test_record_gather_rejects_invalid_shapes(words, in_words, out_words, selectors) -> None:
    with pytest.raises(ValueError):
        gather_records(words, in_words=in_words, out_words=out_words, selectors=selectors)


@pytest.mark.parametrize(
    ("op", "constant", "expected"),
    [
        (PredicateOp.EQ, 5, [5, 5]),
        (PredicateOp.NE, 5, [7, -2, 11, 0, -100, 2**31 - 1, -(2**31)]),
        (PredicateOp.LT, 0, [-2, -100, -(2**31)]),
        (PredicateOp.LE, 0, [-2, 0, -100, -(2**31)]),
        (PredicateOp.GT, 5, [7, 11, 2**31 - 1]),
        (PredicateOp.GE, 5, [7, 5, 11, 5, 2**31 - 1]),
    ],
)
def test_single_predicate_ops(op: PredicateOp, constant: int, expected: list[int]) -> None:
    assert filter_values(_VALUES, FilterPredicate(op0=op, constant0=constant)) == expected


def test_and_combine_is_range_filter() -> None:
    predicate = FilterPredicate(op0=PredicateOp.GE, constant0=0, combine=PredicateCombine.AND, op1=PredicateOp.LT, constant1=10)
    assert filter_values(_VALUES, predicate) == [7, 5, 5, 0]


def test_or_combine_is_union() -> None:
    predicate = FilterPredicate(op0=PredicateOp.LT, constant0=-50, combine=PredicateCombine.OR, op1=PredicateOp.GT, constant1=50)
    assert filter_values(_VALUES, predicate) == [-100, 2**31 - 1, -(2**31)]


def test_empty_selection_and_empty_input() -> None:
    predicate = FilterPredicate(op0=PredicateOp.GT, constant0=2**31 - 1)
    assert filter_values(_VALUES, predicate) == []
    assert filter_values([], FilterPredicate(op0=PredicateOp.EQ, constant0=0)) == []


def test_order_is_preserved() -> None:
    predicate = FilterPredicate(op0=PredicateOp.NE, constant0=0)
    result = filter_values(_VALUES, predicate)
    assert result == [value for value in _VALUES if value != 0]


def test_constants_must_fit_int32() -> None:
    with pytest.raises(ValueError, match="int32"):
        FilterPredicate(op0=PredicateOp.EQ, constant0=2**31)


def test_map_add_sub_mul() -> None:
    values = [7, -2, 5, 0, -100]
    assert map_values(values, MapConfig(op=MapOp.ADD, constant=3)) == [10, 1, 8, 3, -97]
    assert map_values(values, MapConfig(op=MapOp.SUB, constant=3)) == [4, -5, 2, -3, -103]
    assert map_values(values, MapConfig(op=MapOp.MUL, constant=-2)) == [-14, 4, -10, 0, 200]


def test_map_wraps_int32() -> None:
    assert map_values([2**31 - 1], MapConfig(op=MapOp.ADD, constant=1)) == [-(2**31)]
    assert map_values([-(2**31)], MapConfig(op=MapOp.SUB, constant=1)) == [2**31 - 1]
    assert map_values([2**30], MapConfig(op=MapOp.MUL, constant=4)) == [0]


def test_map_mulshr_is_bucket_derivation() -> None:
    # seconds -> minutes via multiply-shift: floor(v * 139811 / 2**23) == v // 60,
    # exact over the full day range [0, 86400)
    config = MapConfig(op=MapOp.MULSHR, constant=139811, shift=23)
    seconds = [0, 59, 60, 61, 3599, 3600, 34200, 57600, 86399]
    assert map_values(seconds, config) == [s // 60 for s in seconds]


def test_map_mulshr_arithmetic_shift_on_negative() -> None:
    assert map_values([-7], MapConfig(op=MapOp.MULSHR, constant=1, shift=1)) == [-4]


def test_map_shift_and_bitwise_key_packing() -> None:
    # pack a small id into the high bits then OR in a low field
    assert map_values([5], MapConfig(op=MapOp.SHL, shift=16)) == [5 << 16]
    assert map_values([0x1234_0000 - 2**31], MapConfig(op=MapOp.AND, constant=0x0FF0_0000)) == [0x0230_0000]
    assert map_values([1 << 16], MapConfig(op=MapOp.OR, constant=0x2A)) == [(1 << 16) | 0x2A]


def test_map_order_and_count_preserved() -> None:
    values = list(range(-5, 6))
    result = map_values(values, MapConfig(op=MapOp.ADD, constant=0))
    assert result == values


def test_map_config_validation() -> None:
    with pytest.raises(ValueError, match="int32"):
        MapConfig(op=MapOp.ADD, constant=2**31)
    with pytest.raises(ValueError, match="shift"):
        MapConfig(op=MapOp.SHL, shift=32)


def test_grouped_aggregate_consecutive_keys() -> None:
    rows = [(1, 10), (1, 5), (1, 20), (2, -3), (3, 7), (3, 7)]
    assert grouped_aggregate_rows(rows) == [
        GroupAggregate(key=1, first=10, last=20, minimum=5, maximum=20, total=35, count=3),
        GroupAggregate(key=2, first=-3, last=-3, minimum=-3, maximum=-3, total=-3, count=1),
        GroupAggregate(key=3, first=7, last=7, minimum=7, maximum=7, total=14, count=2),
    ]


def test_grouped_aggregate_reappearing_key_opens_new_partial() -> None:
    rows = [(5, 1), (7, 2), (5, 3)]
    results = grouped_aggregate_rows(rows)
    assert [group.key for group in results] == [5, 7, 5]
    assert results[0].total == 1
    assert results[2].total == 3


def test_grouped_aggregate_single_group_and_empty() -> None:
    assert grouped_aggregate_rows([]) == []
    ohlcv = grouped_aggregate_rows([(9, 100), (9, 105), (9, 95), (9, 102)])
    assert ohlcv == [GroupAggregate(key=9, first=100, last=102, minimum=95, maximum=105, total=402, count=4)]


def test_grouped_aggregate_int64_sum_of_int32_extremes() -> None:
    rows = [(1, 2**31 - 1)] * 4
    result = grouped_aggregate_rows(rows)[0]
    assert result.total == 4 * (2**31 - 1)
    assert result.count == 4


def test_filter_rows_on_value_lane_keeps_pairs_aligned() -> None:
    rows = [(570, 100), (570, 0), (571, -5), (571, 99)]
    predicate = FilterPredicate(op0=PredicateOp.GT, constant0=0)
    assert filter_rows(rows, predicate) == [(570, 100), (571, 99)]


def test_filter_rows_on_key_lane() -> None:
    rows = [(570, 100), (571, 99), (572, 5)]
    predicate = FilterPredicate(op0=PredicateOp.LE, constant0=571)
    assert filter_rows(rows, predicate, on_key=True) == [(570, 100), (571, 99)]


def test_map_rows_key_lane_derives_buckets_value_passthrough() -> None:
    # (time_seconds, price) -> (minute_bucket, price)
    config = MapConfig(op=MapOp.MULSHR, constant=139811, shift=23)
    rows = [(34200, 100), (34259, 105), (34260, 95)]
    assert map_rows(rows, config, on_key=True) == [(570, 100), (570, 105), (571, 95)]


def test_map_rows_value_lane_key_passthrough() -> None:
    config = MapConfig(op=MapOp.ADD, constant=1000)
    assert map_rows([(1, 5), (2, -5)], config) == [(1, 1005), (2, 995)]


def test_ohlcv_graph_golden_composition() -> None:
    # filter eligibility -> derive minute buckets -> bars
    trades = [(34200, 100), (34210, 0), (34259, 105), (34260, 95), (34261, 102)]
    eligible = filter_rows(trades, FilterPredicate(op0=PredicateOp.GT, constant0=0))
    keyed = map_rows(eligible, MapConfig(op=MapOp.MULSHR, constant=139811, shift=23), on_key=True)
    bars = grouped_aggregate_rows(keyed)
    assert [bar.key for bar in bars] == [570, 571]
    assert bars[0] == GroupAggregate(key=570, first=100, last=105, minimum=100, maximum=105, total=205, count=2)
    assert bars[1] == GroupAggregate(key=571, first=95, last=102, minimum=95, maximum=102, total=197, count=2)


def test_bar_aggregate_fuses_price_stats_and_volume() -> None:
    rows = [
        (570, 100, 10, 500_000),
        (570, 105, 3, 0),
        (570, 95, 2, 250_000),
        (571, 102, 7, 0),
    ]
    bars = bar_aggregate_rows(rows)
    assert bars == [
        BarAggregate(key=570, open=100, close=95, low=95, high=105, shares=15, frac=750_000, count=3),
        BarAggregate(key=571, open=102, close=102, low=102, high=102, shares=7, frac=0, count=1),
    ]


def test_bar_aggregate_matches_composed_goldens() -> None:
    import random

    rng = random.Random(11)
    rows = []
    key = 0
    for _ in range(500):
        key += rng.random() < 0.1
        rows.append((key, rng.randint(-1000, 1000), rng.randint(0, 10_000), rng.randint(0, 999_999)))
    fused = bar_aggregate_rows(rows)
    prices = grouped_aggregate_rows([(k, p) for k, p, _, _ in rows])
    shares = grouped_aggregate_rows([(k, s) for k, _, s, _ in rows])
    fracs = grouped_aggregate_rows([(k, f) for k, _, _, f in rows])
    assert [b.key for b in fused] == [g.key for g in prices]
    for bar, price, share, frac in zip(fused, prices, shares, fracs):
        assert (bar.open, bar.close, bar.low, bar.high) == (price.first, price.last, price.minimum, price.maximum)
        assert bar.shares == share.total
        assert bar.frac == frac.total
        assert bar.count == price.count


# (key, value, aux0, aux1) = (shipdate-bucket, extendedprice, discount, quantity)
_QUAD_ROWS = [
    (568, 1000, 5, 10),
    (569, 2000, 6, 30),
    (568, 1500, 7, 23),
    (570, -500, 6, 24),
    (568, 3000, 4, 1),
]


def test_row_predicate_single_term_on_each_field() -> None:
    assert filter_rows_by_predicate(_QUAD_ROWS, RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568))) == [
        (568, 1000, 5, 10),
        (568, 1500, 7, 23),
        (568, 3000, 4, 1),
    ]
    assert filter_rows_by_predicate(_QUAD_ROWS, RowPredicateConfig(term0=RowPredicateTerm(field=RowField.VALUE, op=PredicateOp.LT, constant=0))) == [
        (570, -500, 6, 24)
    ]
    assert filter_rows_by_predicate(_QUAD_ROWS, RowPredicateConfig(term0=RowPredicateTerm(field=RowField.AUX0, op=PredicateOp.GE, constant=7))) == [
        (568, 1500, 7, 23)
    ]
    assert filter_rows_by_predicate(_QUAD_ROWS, RowPredicateConfig(term0=RowPredicateTerm(field=RowField.AUX1, op=PredicateOp.NE, constant=30))) == [
        row for row in _QUAD_ROWS if row[3] != 30
    ]


def test_row_predicate_three_term_tree_is_the_q6_where() -> None:
    # shipdate bucket EQ AND discount GE lo AND quantity LT 24
    config = RowPredicateConfig(
        term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=568),
        combine01=PredicateCombine.AND,
        term1=RowPredicateTerm(field=RowField.AUX0, op=PredicateOp.GE, constant=5),
        combine2=PredicateCombine.AND,
        term2=RowPredicateTerm(field=RowField.AUX1, op=PredicateOp.LT, constant=24),
    )
    assert filter_rows_by_predicate(_QUAD_ROWS, config) == [(568, 1000, 5, 10), (568, 1500, 7, 23)]


def test_row_predicate_or_of_and_levels() -> None:
    # (value > 2000 OR key == 570) AND aux1 < 30
    config = RowPredicateConfig(
        term0=RowPredicateTerm(field=RowField.VALUE, op=PredicateOp.GT, constant=2000),
        combine01=PredicateCombine.OR,
        term1=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=570),
        combine2=PredicateCombine.AND,
        term2=RowPredicateTerm(field=RowField.AUX1, op=PredicateOp.LT, constant=30),
    )
    assert filter_rows_by_predicate(_QUAD_ROWS, config) == [(570, -500, 6, 24), (568, 3000, 4, 1)]


def test_row_predicate_validation() -> None:
    with pytest.raises(ValueError, match="int32"):
        RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=2**31)
    with pytest.raises(ValueError, match="term1"):
        RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=0), combine01=PredicateCombine.AND)
    with pytest.raises(ValueError, match="term2"):
        RowPredicateConfig(term0=RowPredicateTerm(field=RowField.KEY, op=PredicateOp.EQ, constant=0), combine2=PredicateCombine.OR)


def test_row_map_q6_revenue_is_fixed_point_mulshr() -> None:
    config = RowMapConfig(op=MapOp.MULSHR, dst=RowField.VALUE, src_a=RowField.VALUE, src_b=RowField.AUX0, shift=15)
    rows = [(568, 1000, 5 << 10, 10)]
    # 1000 * 5120 >> 15 = 156 (floor)
    assert map_row_fields(rows, config) == [(568, 156, 5 << 10, 10)]


def test_row_map_other_fields_pass_through() -> None:
    config = RowMapConfig(op=MapOp.ADD, dst=RowField.AUX1, src_a=RowField.KEY, src_b=RowField.VALUE)
    for row, mapped in zip(_QUAD_ROWS, map_row_fields(_QUAD_ROWS, config)):
        assert mapped[:3] == row[:3]
        assert mapped[3] == ((row[0] + row[1] + 2**31) % 2**32) - 2**31


def test_row_map_wraps_int32() -> None:
    config = RowMapConfig(op=MapOp.MUL, dst=RowField.KEY, src_a=RowField.KEY, src_b=RowField.KEY)
    assert map_row_fields([(2**16, 0, 0, 0)], config) == [(0, 0, 0, 0)]


def test_mask_dispatch_routes_by_two_complement_low_bits() -> None:
    # the pair-key partition moved into the router: key & (N-1), negative keys
    # masking exactly like the hardware bit-select (-1 & 3 == 3)
    rows = [(-1, 1, 1, 0), (-4, 2, 2, 0), (4, 3, 3, 0), (7, 4, 4, 0)]
    parts = mask_dispatch_rows(rows, num_partitions=4)
    assert parts == [[(-4, 2, 2, 0), (4, 3, 3, 0)], [], [], [(-1, 1, 1, 0), (7, 4, 4, 0)]]


def test_mask_dispatch_rejects_non_power_of_two() -> None:
    with pytest.raises(ValueError, match="power of two"):
        mask_dispatch_rows([(1, 2, 3, 4)], num_partitions=3)


def test_exact_branch_dispatch_routes_in_range_and_drops_the_rest() -> None:
    # q19's disjunct branches: dest = key - 1 for keys in [1, N], branch 0 and
    # every out-of-range key (negatives drop via their unsigned image) cost
    # zero lane deliveries; no power-of-two requirement
    rows = [(1, 1, 0, 0), (0, 2, 0, 0), (3, 3, 0, 0), (4, 4, 0, 0), (-1, 5, 0, 0), (2, 6, 0, 0), (0, 7, 0, 0)]
    parts = mask_dispatch_rows(rows, num_partitions=3, exact_branch=True)
    assert parts == [[(1, 1, 0, 0)], [(2, 6, 0, 0)], [(3, 3, 0, 0)]]


def test_exact_branch_dispatch_preserves_order_within_a_branch() -> None:
    rows = [(2, i, 0, 0) for i in range(5)] + [(0, 99, 0, 0)]
    parts = mask_dispatch_rows(rows, num_partitions=2, exact_branch=True)
    assert parts == [[], [(2, i, 0, 0) for i in range(5)]]


def test_exact_branch_dispatch_rejects_non_positive_partitions() -> None:
    with pytest.raises(ValueError, match="positive"):
        mask_dispatch_rows([(1, 2, 3, 4)], num_partitions=0, exact_branch=True)


def test_stride_distribute_routes_by_static_slot_group() -> None:
    rows = [(i, i + 10, i + 20, i + 30) for i in range(10)]
    parts = stride_distribute_rows(rows, rows_per_beat=2, num_partitions=4)
    assert parts == (
        [rows[0], rows[4], rows[8]],
        [rows[2], rows[6]],
        [rows[1], rows[5], rows[9]],
        [rows[3], rows[7]],
    )


def test_stride_distribute_supports_one_lane_per_slot() -> None:
    rows = [(i, 0, 0, 0) for i in range(7)]
    parts = stride_distribute_rows(rows, rows_per_beat=4, num_partitions=4)
    assert parts == ([rows[0], rows[4]], [rows[1], rows[5]], [rows[2], rows[6]], [rows[3]])


@pytest.mark.parametrize(("rows_per_beat", "num_partitions"), [(0, 4), (2, 0), (4, 6)])
def test_stride_distribute_rejects_invalid_lane_shape(rows_per_beat: int, num_partitions: int) -> None:
    with pytest.raises(ValueError):
        stride_distribute_rows([], rows_per_beat=rows_per_beat, num_partitions=num_partitions)


def test_pack_splitters_is_msb_first_reversed_hex() -> None:
    # cfg_splitters packs splitter i at [i*32 +: 32]; SV concatenation is
    # MSB-first, so the highest-index splitter is emitted first. Signed int32
    # values pack as their two's-complement hex.
    assert pack_splitters((10, 20, 30)) == "{32'h0000001E, 32'h00000014, 32'h0000000A}"
    assert pack_splitters((-(2**31), -1, 2**31 - 1)) == "{32'h7FFFFFFF, 32'hFFFFFFFF, 32'h80000000}"
    assert pack_splitters((7,)) == "{32'h00000007}"


def test_pack_splitters_rejects_out_of_signed_int32_range() -> None:
    with pytest.raises(ValueError, match="signed int32"):
        pack_splitters((10, 20, 2**31))
    with pytest.raises(ValueError, match="signed int32"):
        pack_splitters((-(2**31) - 1, 10))


def test_row_map_shift_validation() -> None:
    with pytest.raises(ValueError, match="shift"):
        RowMapConfig(op=MapOp.MULSHR, dst=RowField.KEY, src_a=RowField.KEY, src_b=RowField.KEY, shift=32)


def test_field_sum_single_field_mask_is_q6() -> None:
    result = field_sum_rows(_QUAD_ROWS, field_mask=0b0010)
    assert result == FieldSumAggregate(field_mask=0b0010, sums=(7000,), count=5)
    assert result.record_words() == [7000, 5]


def test_field_sum_full_mask_is_the_q1_shape() -> None:
    result = field_sum_rows(_QUAD_ROWS, field_mask=0b1111)
    assert result.sums == (
        sum(row[0] for row in _QUAD_ROWS),
        sum(row[1] for row in _QUAD_ROWS),
        sum(row[2] for row in _QUAD_ROWS),
        sum(row[3] for row in _QUAD_ROWS),
    )
    assert result.count == 5
    assert len(result.record_words()) == 5


def test_field_sum_negative_sum_encodes_twos_complement() -> None:
    result = field_sum_rows([(0, -1000, 0, 0), (0, -2000, 0, 0)], field_mask=0b0010)
    assert result.sums == (-3000,)
    assert result.record_words() == [(-3000) & 0xFFFF_FFFF_FFFF_FFFF, 2]


def test_field_sum_int32_extremes_stay_exact() -> None:
    rows = [(-(2**31), 2**31 - 1, 0, 0)] * 6
    result = field_sum_rows(rows, field_mask=0b0011)
    assert result.sums == (6 * -(2**31), 6 * (2**31 - 1))


def test_field_sum_empty_batch_and_mask_validation() -> None:
    assert field_sum_rows([], field_mask=0b0001) == FieldSumAggregate(field_mask=0b0001, sums=(0,), count=0)
    with pytest.raises(ValueError, match="field_mask"):
        field_sum_rows(_QUAD_ROWS, field_mask=0)
    with pytest.raises(ValueError, match="field_mask"):
        field_sum_rows(_QUAD_ROWS, field_mask=16)


_GROUPED_ROWS = [
    (3, 1000, 5, 10),
    (3, 2000, 6, 30),
    (7, -500, 6, 24),
    (9, 3000, 4, 1),
    (9, 1, 2, 3),
]


def test_grouped_field_sum_groups_consecutive_equal_keys() -> None:
    result = grouped_field_sum_rows(_GROUPED_ROWS, field_mask=0b1010)
    assert result == [
        GroupedFieldSumAggregate(key=3, field_mask=0b1010, sums=(3000, 40), count=2),
        GroupedFieldSumAggregate(key=7, field_mask=0b1010, sums=(-500, 24), count=1),
        GroupedFieldSumAggregate(key=9, field_mask=0b1010, sums=(3001, 4), count=2),
    ]


def test_grouped_field_sum_record_words_carry_key_sums_count() -> None:
    groups = grouped_field_sum_rows(_GROUPED_ROWS, field_mask=0b0010)
    assert groups[0].record_words() == [3, 3000, 2]
    negative = grouped_field_sum_rows([(-3, -1000, 0, 0)], field_mask=0b0010)
    # the key word is the signed key sign-extended to 64 bits
    assert negative[0].record_words() == [(-3) & 0xFFFF_FFFF_FFFF_FFFF, (-1000) & 0xFFFF_FFFF_FFFF_FFFF, 1]


def test_grouped_field_sum_reappearing_key_opens_a_new_group() -> None:
    result = grouped_field_sum_rows([(5, 10, 0, 0), (6, 20, 0, 0), (5, 30, 0, 0)], field_mask=0b0010)
    assert [group.key for group in result] == [5, 6, 5]
    assert [group.sums for group in result] == [(10,), (20,), (30,)]


def test_grouped_field_sum_int64_accumulation_and_masks() -> None:
    rows = [(1, 2**31 - 1, -(2**31), 7)] * 4
    result = grouped_field_sum_rows(rows, field_mask=0b1110)
    assert result[0].sums == (4 * (2**31 - 1), 4 * -(2**31), 28)
    assert result[0].count == 4


def test_grouped_field_sum_empty_and_mask_validation() -> None:
    assert grouped_field_sum_rows([], field_mask=0b0001) == []
    with pytest.raises(ValueError, match="field_mask"):
        grouped_field_sum_rows(_GROUPED_ROWS, field_mask=0)
    with pytest.raises(ValueError, match="field_mask"):
        grouped_field_sum_rows(_GROUPED_ROWS, field_mask=16)


_MEMBERSHIP_ROWS = [
    (7, 1000, 5, 10),
    (100, 2000, 6, 30),
    (7, 1500, 7, 23),
    (191, -500, 6, 24),
    (63, 3000, 4, 1),
]


def test_membership_semi_keeps_members_anti_keeps_non_members() -> None:
    build = MembershipBuildSet.from_keys({7, 63}, key_space=192)
    assert filter_rows_by_membership(_MEMBERSHIP_ROWS, build, mode=MembershipMode.SEMI) == [
        (7, 1000, 5, 10),
        (7, 1500, 7, 23),
        (63, 3000, 4, 1),
    ]
    assert filter_rows_by_membership(_MEMBERSHIP_ROWS, build, mode=MembershipMode.ANTI) == [
        (100, 2000, 6, 30),
        (191, -500, 6, 24),
    ]


def test_membership_empty_and_full_sets() -> None:
    empty = MembershipBuildSet.from_keys((), key_space=64)
    assert filter_rows_by_membership(_MEMBERSHIP_ROWS, empty, mode=MembershipMode.SEMI) == []
    assert filter_rows_by_membership(_MEMBERSHIP_ROWS, empty, mode=MembershipMode.ANTI) == _MEMBERSHIP_ROWS
    full = MembershipBuildSet.from_keys(range(192), key_space=192)
    # every in-range key is a member; all _MEMBERSHIP_ROWS keys are in [0, 192)
    assert filter_rows_by_membership(_MEMBERSHIP_ROWS, full, mode=MembershipMode.SEMI) == _MEMBERSHIP_ROWS
    assert filter_rows_by_membership(_MEMBERSHIP_ROWS, full, mode=MembershipMode.ANTI) == []


def test_membership_out_of_range_keys_are_non_members() -> None:
    build = MembershipBuildSet.from_keys({0, 63}, key_space=64)
    rows = [(0, 0, 0, 0), (63, 0, 0, 0), (64, 0, 0, 0), (-1, 0, 0, 0), (-(2**31), 0, 0, 0), (2**31 - 1, 0, 0, 0)]
    # exact: in-range members kept by semi; every out-of-range key (>= key_space
    # or negative) is definitionally a non-member
    assert filter_rows_by_membership(rows, build, mode=MembershipMode.SEMI) == [(0, 0, 0, 0), (63, 0, 0, 0)]
    assert build.member(0) and build.member(63)
    assert not build.member(64) and not build.member(-1) and not build.member(2**31 - 1)


def test_membership_image_words_round_trip_is_exact() -> None:
    build = MembershipBuildSet.from_keys({0, 1, 63, 64, 65, 4095}, key_space=4096)
    words = build.image_words()
    assert len(words) == build.word_count == 64
    # bit b of word w encodes id 64*w + b
    assert words[0] & 1 and words[0] & 2 and words[0] & (1 << 63)  # ids 0, 1, 63
    assert words[1] & 1 and words[1] & 2  # ids 64, 65
    assert words[63] & (1 << 63)  # id 4095
    assert MembershipBuildSet.from_image_words(words, key_space=4096) == build


def test_membership_build_set_validation() -> None:
    with pytest.raises(ValueError, match="multiple of 64"):
        MembershipBuildSet.from_keys((), key_space=100)
    with pytest.raises(ValueError, match="multiple of 64"):
        MembershipBuildSet.from_keys((), key_space=0)
    with pytest.raises(ValueError, match="outside key space"):
        MembershipBuildSet.from_keys({64}, key_space=64)
    with pytest.raises(ValueError, match="outside key space"):
        MembershipBuildSet.from_keys({-1}, key_space=64)
    with pytest.raises(ValueError, match="expected"):
        MembershipBuildSet.from_image_words([0, 0], key_space=64)


def test_membership_kind_and_mode_are_one_hot_config() -> None:
    # kind zero is reserved (unconfigured fails explicitly); bloom is a
    # reserved future kind, not built in v0
    assert MembershipKind.EXACT_BITMAP == 1
    assert MembershipKind.BLOOM == 2
    assert int(MembershipMode.SEMI) == 0 and int(MembershipMode.ANTI) == 1


def test_membership_order_is_preserved() -> None:
    build = MembershipBuildSet.from_keys({7}, key_space=64)
    rows = [(7, i, 0, 0) for i in range(5)]
    assert filter_rows_by_membership(rows, build, mode=MembershipMode.SEMI) == rows


_JOIN_BUILD = [
    (7, 100, 101, 102),
    (100, 200, 201, 202),
    (63, 300, 301, 302),
]
_JOIN_PROBE = [
    (7, 1000, 1001, 1002),  # hit
    (999, 2000, 2001, 2002),  # miss
    (63, 3000, 3001, 3002),  # hit
    (100, 4000, 4001, 4002),  # hit
]
_CARRY_BUILD = BroadcastJoinConfig(out0=JoinSource.B0, out1=JoinSource.B1, out2=JoinSource.B2)


def test_broadcast_join_inner_join_carries_build_payload() -> None:
    out = broadcast_hash_join_rows(_JOIN_BUILD, _JOIN_PROBE, out_sel=_CARRY_BUILD, key_space=1024)
    # only the three hits emit, in probe order, carrying the build payload
    assert out == [(7, 100, 101, 102), (63, 300, 301, 302), (100, 200, 201, 202)]


def test_broadcast_join_field_select_mixes_build_and_probe() -> None:
    # q3 shape: (key, b0, v0, b2)
    cfg = BroadcastJoinConfig(out0=JoinSource.B0, out1=JoinSource.V0, out2=JoinSource.B2)
    out = broadcast_hash_join_rows(_JOIN_BUILD, _JOIN_PROBE, out_sel=cfg, key_space=1024)
    assert out == [(7, 100, 1000, 102), (63, 300, 3000, 302), (100, 200, 4000, 202)]


def test_broadcast_join_carry_probe_columns() -> None:
    cfg = BroadcastJoinConfig(out0=JoinSource.V0, out1=JoinSource.V1, out2=JoinSource.V2)
    out = broadcast_hash_join_rows(_JOIN_BUILD, _JOIN_PROBE, out_sel=cfg, key_space=1024)
    assert out == [(7, 1000, 1001, 1002), (63, 3000, 3001, 3002), (100, 4000, 4001, 4002)]


def test_broadcast_join_last_writer_wins_on_duplicate_build_key() -> None:
    build = [(7, 11, 12, 13), (7, 21, 22, 23), (9, 90, 91, 92)]
    out = broadcast_hash_join_rows(build, [(7, 0, 0, 0), (9, 0, 0, 0)], out_sel=_CARRY_BUILD, key_space=1024)
    assert out == [(7, 21, 22, 23), (9, 90, 91, 92)]


def test_broadcast_join_all_misses_emit_nothing() -> None:
    out = broadcast_hash_join_rows(_JOIN_BUILD, [(1, 0, 0, 0), (2, 0, 0, 0)], out_sel=_CARRY_BUILD, key_space=1024)
    assert out == []


def test_broadcast_join_int32_wraparound_in_payload() -> None:
    build = [(5, 2**31, -1, 0)]
    out = broadcast_hash_join_rows(build, [(5, 0, 0, 0)], out_sel=_CARRY_BUILD, key_space=1024)
    # 2**31 wraps to -2**31 in the int32 domain
    assert out == [(5, -(2**31), -1, 0)]


def test_broadcast_join_key_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="build key .* outside key space"):
        broadcast_hash_join_rows([(1024, 0, 0, 0)], [], out_sel=_CARRY_BUILD, key_space=1024)
    with pytest.raises(ValueError, match="probe key .* outside key space"):
        broadcast_hash_join_rows([], [(1024, 0, 0, 0)], out_sel=_CARRY_BUILD, key_space=1024)
    with pytest.raises(ValueError, match="probe key .* outside key space"):
        broadcast_hash_join_rows([], [(-1, 0, 0, 0)], out_sel=_CARRY_BUILD, key_space=1024)


def test_broadcast_join_config_validation() -> None:
    assert int(JoinSource.B0) == 0 and int(JoinSource.V2) == 5
    # pydantic enum validation rejects non-members at the field boundary
    # (ValidationError is-a ValueError)
    with pytest.raises(ValueError, match="0, 1, 2, 3, 4 or 5"):
        BroadcastJoinConfig(out0=6, out1=JoinSource.B1, out2=JoinSource.B2)
    with pytest.raises(ValueError, match="0, 1, 2, 3, 4 or 5"):
        BroadcastJoinConfig(out0=JoinSource.B0, out1=JoinSource.B1, out2=7)


def test_broadcast_join_bounded_key_space_and_order_preserved() -> None:
    build = [(0, 1, 2, 3), (63, 4, 5, 6)]
    probe = [(63, 0, 0, 0), (0, 0, 0, 0)]  # reversed order preserved on the probe side
    out = broadcast_hash_join_rows(build, probe, out_sel=_CARRY_BUILD, key_space=64)
    assert out == [(63, 4, 5, 6), (0, 1, 2, 3)]


def _pack(specs: list[UnpackFieldSpec], fields: tuple[int, int, int, int]) -> int:
    """Pack four field values into one word at the specs' offsets/widths
    (two's-complement within each width)."""
    word = 0
    for spec, value in zip(specs, fields):
        word |= (value & ((1 << spec.width) - 1)) << spec.offset
    return word


# q6-like packing: (shipdate-bucket, extendedprice, discount, quantity) into 59
# bits — 16 + 25 + 10 + 8, offsets 0/16/41/51
_Q6_SPECS = [
    UnpackFieldSpec(offset=0, width=16),
    UnpackFieldSpec(offset=16, width=25, signed=True),
    UnpackFieldSpec(offset=41, width=10),
    UnpackFieldSpec(offset=51, width=8),
]
# q1-like packing: 63 bits — 10 + 32 + 13 + 8, offsets 0/10/42/55
_Q1_SPECS = [
    UnpackFieldSpec(offset=0, width=10),
    UnpackFieldSpec(offset=10, width=32, signed=True),
    UnpackFieldSpec(offset=42, width=13),
    UnpackFieldSpec(offset=55, width=8),
]


def test_unpack_single_word_exact_quad_row_tuple() -> None:
    specs = [
        UnpackFieldSpec(offset=0, width=8),
        UnpackFieldSpec(offset=8, width=8),
        UnpackFieldSpec(offset=16, width=8),
        UnpackFieldSpec(offset=24, width=8),
    ]
    word = _pack(specs, (0x11, 0x22, 0x33, 0x44))
    assert unpack_rows([word], specs) == [(0x11, 0x22, 0x33, 0x44)]


def test_unpack_zero_extend_keeps_field_positive() -> None:
    # width-8 unsigned field with the high bit set stays positive (0..255)
    specs = [UnpackFieldSpec(offset=o, width=8) for o in (0, 8, 16, 24)]
    word = _pack(specs, (0xFF, 0x80, 0x7F, 0x00))
    assert unpack_rows([word], specs) == [(0xFF, 0x80, 0x7F, 0x00)]


def test_unpack_sign_extend_recovers_negative() -> None:
    # width-8 signed field: 0xFF -> -1, 0x80 -> -128, 0x7F -> 127
    specs = [UnpackFieldSpec(offset=o, width=8, signed=True) for o in (0, 8, 16, 24)]
    word = _pack(specs, (-1, -128, 127, 0))
    assert unpack_rows([word], specs) == [(-1, -128, 127, 0)]


def test_unpack_fields_cross_byte_boundaries() -> None:
    # unaligned offsets and widths that straddle byte lanes
    specs = [
        UnpackFieldSpec(offset=3, width=13),
        UnpackFieldSpec(offset=16, width=17, signed=True),
        UnpackFieldSpec(offset=33, width=11),
        UnpackFieldSpec(offset=44, width=20, signed=True),
    ]
    fields = (0x1ABC & 0x1FFF, -12345, 0x5A5, -7)
    word = _pack(specs, fields)
    out = unpack_rows([word], specs)
    assert out == [(0x1ABC & 0x1FFF, -12345, 0x5A5, -7)]


def test_unpack_boundary_widths_one_and_thirtytwo() -> None:
    # width 1 (signed 1 => -1/0, unsigned 1 => 1/0) and width 32 (full int32)
    specs = [
        UnpackFieldSpec(offset=0, width=1, signed=True),
        UnpackFieldSpec(offset=1, width=1),
        UnpackFieldSpec(offset=2, width=32, signed=True),
        UnpackFieldSpec(offset=34, width=30),
    ]
    word = _pack(specs, (-1, 1, -(2**31), (1 << 30) - 1))
    assert unpack_rows([word], specs) == [(-1, 1, -(2**31), (1 << 30) - 1)]


def test_unpack_width32_unsigned_wraps_to_signed_int32() -> None:
    # a full-width unsigned field with the top bit set reads back as signed
    specs = [
        UnpackFieldSpec(offset=0, width=32),
        UnpackFieldSpec(offset=32, width=32, signed=True),
        UnpackFieldSpec(offset=0, width=1),
        UnpackFieldSpec(offset=1, width=1),
    ]
    word = (0xFFFF_FFFF << 32) | 0x8000_0000
    out = unpack_rows([word], specs)
    # low 32 unsigned 0x80000000 -> -2**31; high 32 signed 0xFFFFFFFF -> -1
    assert out == [(-(2**31), -1, 0, 0)]


def test_unpack_q6_like_packing() -> None:
    rows = [(568, 1000, 5, 10), (569, -500, 6, 30), (570, 12000, 1023, 255)]
    words = [_pack(_Q6_SPECS, row) for row in rows]
    assert unpack_rows(words, _Q6_SPECS) == rows


def test_unpack_q1_like_packing_and_order_preserved() -> None:
    rows = [(3, 2**31 - 1, 8191, 7), (3, -(2**31), 0, 255), (9, -1, 4096, 1)]
    words = [_pack(_Q1_SPECS, row) for row in rows]
    assert unpack_rows(words, _Q1_SPECS) == rows


def test_unpack_field_spec_config_rejects() -> None:
    with pytest.raises(ValueError, match="width"):
        UnpackFieldSpec(offset=0, width=0)
    with pytest.raises(ValueError, match="width"):
        UnpackFieldSpec(offset=0, width=33)
    with pytest.raises(ValueError, match="offset \\+ width"):
        UnpackFieldSpec(offset=40, width=32)
    with pytest.raises(ValueError, match="offset"):
        UnpackFieldSpec(offset=64, width=1)


def test_unpack_requires_four_specs() -> None:
    with pytest.raises(ValueError, match="four field specs"):
        unpack_rows([0], [UnpackFieldSpec(offset=0, width=8)])


def test_unpack_empty_input() -> None:
    assert unpack_rows([], _Q6_SPECS) == []


def test_unpack_bypass_returns_quad_rows_unchanged() -> None:
    # BYPASS: input is already 16-byte quad rows; the tile forwards them
    # byte-identically, so the golden is the identity (no bit-extract)
    rows = [(0x11, 0x22, 0x33, 0x44), (568, -500, 6, 30), (-1, 2**31 - 1, 0, 255)]
    assert unpack_rows(rows, bypass=True) == rows


def test_unpack_bypass_ignores_field_specs() -> None:
    # field descriptors are unused in bypass mode: identity regardless
    rows = [(3, -(2**31), 4096, 1)]
    assert unpack_rows(rows, _Q6_SPECS, bypass=True) == rows == unpack_rows(rows, bypass=True)


def test_unpack_bypass_empty_input() -> None:
    assert unpack_rows([], bypass=True) == []


def test_bars_are_a_selection_over_the_generic_field_aggregate() -> None:
    # the monolith decomposition: the generic multi-field grouped aggregator
    # (first/last/min/max/sum per field + count) + an OHLCV field/stat selection
    # reproduce the bar-aggregation golden exactly
    import random

    from dau_core.operators import bars_from_field_aggregate, grouped_field_aggregate_rows

    rng = random.Random(11)
    rows = [(rng.randint(0, 6), rng.randint(-40, 40), rng.randint(0, 90), rng.randint(-25, 25)) for _ in range(300)]
    rows.sort(key=lambda r: r[0])  # ordered close-out precondition
    rows += [(0, 5, 2, 1), (0, 8, 3, -2)]  # a reappearing key -> a fresh partial group

    assert bars_from_field_aggregate(grouped_field_aggregate_rows(rows)) == bar_aggregate_rows(rows)


def test_grouped_field_aggregate_per_field_stats() -> None:
    from dau_core.operators import FieldStats, grouped_field_aggregate_rows

    rows = [(7, 10, 1, -1), (7, 5, 2, -3), (7, 8, 3, 4)]  # one group
    (group,) = grouped_field_aggregate_rows(rows)
    assert group.key == 7 and group.count == 3
    assert group.fields[0] == FieldStats(first=10, last=8, minimum=5, maximum=10, total=23)
    assert group.fields[1] == FieldStats(first=1, last=3, minimum=1, maximum=3, total=6)
    assert group.fields[2] == FieldStats(first=-1, last=4, minimum=-3, maximum=4, total=0)


def test_finance_asof_is_the_generic_keyed_asof_join() -> None:
    # the as-of decomposition: the temporal-finance backward as-of (quote=build
    # {bid,ask}, trade=probe {price,size}) is exactly the generic keyed as-of
    # join over opaque payloads — the finance vocabulary lives in the host merge,
    # not the tile
    from dau_core.operators import keyed_asof_join_rows
    from dau_core.temporal import asof_backward, generate_market_stream, matched_rows

    stream = generate_market_stream(seed=7, num_symbols=3)
    trades, quotes = list(stream.trades), list(stream.quotes)
    merged = [(0, s, t, bid, ask) for s, t, bid, ask in quotes] + [(1, s, t, p, sz) for s, t, p, sz in trades]
    merged.sort(key=lambda r: (r[1], r[2], r[0]))  # (key, time), build before probe

    for tol in (None, 3, 10):
        generic = keyed_asof_join_rows(merged, tolerance=tol)
        st = sorted(trades, key=lambda r: (r[0], r[1]))
        sq = sorted(quotes, key=lambda r: (r[0], r[1]))
        finance = [tuple(r) for r in matched_rows(asof_backward(st, sq, tolerance=tol))]
        assert generic == finance


def test_keyed_asof_join_holds_one_build_per_key() -> None:
    from dau_core.operators import keyed_asof_join_rows

    # build b, probe p; a fresh key resets the held build; tolerance drops stale
    records = [
        (0, 5, 100, 10, 12),  # build for key 5
        (1, 5, 105, 1, 2),  # probe -> joins the 100 build
        (0, 5, 110, 11, 13),  # newer build
        (1, 5, 130, 3, 4),  # probe -> joins the 110 build
        (1, 7, 100, 9, 9),  # probe for key 7, no build -> drop
        (0, 7, 101, 50, 52),  # build for key 7
        (1, 7, 101, 5, 6),  # probe at equal time -> joins (allow_eq)
    ]
    assert keyed_asof_join_rows(records) == [
        (5, 105, 1, 2, 10, 12),
        (5, 130, 3, 4, 11, 13),
        (7, 101, 5, 6, 50, 52),
    ]
    assert keyed_asof_join_rows(records, tolerance=5) == [
        (5, 105, 1, 2, 10, 12),  # age 5 <= 5
        (7, 101, 5, 6, 50, 52),  # age 0
    ]


def _wrap32(value: int) -> int:
    value &= (1 << 32) - 1
    return value - (1 << 32) if value >= 1 << 31 else value


def _heap_top_k(rows, k):
    """Operational model of the size-k min-heap tile: fill k, then replace the
    current worst-kept row (smallest key, latest arrival among ties) only when a
    new key is STRICTLY greater. Emit sorted key-desc, arrival-asc. This is the
    algorithm the hardware runs; the equivalence test proves it matches the
    ``streaming_top_k`` spec."""
    kept = []  # (key, arrival, row)
    for arrival, row in enumerate(rows):
        key = _wrap32(row[0])
        if len(kept) < k:
            kept.append((key, arrival, tuple(row)))
            continue
        worst = min(kept, key=lambda e: (e[0], -e[1]))  # contract root: min key, latest arrival
        if key > worst[0]:
            kept.remove(worst)
            kept.append((key, arrival, tuple(row)))
    kept.sort(key=lambda e: (-e[0], e[1]))
    return [row for _, _, row in kept]


def test_streaming_top_k_orders_by_key_desc_limit_k():
    # q3/q10 shape: ORDER BY revenue DESC LIMIT k over (revenue, id, a, b) rows
    rows = [(30, 1, 0, 0), (90, 2, 0, 0), (10, 3, 0, 0), (70, 4, 0, 0), (50, 5, 0, 0)]
    assert streaming_top_k(rows, k=3) == [(90, 2, 0, 0), (70, 4, 0, 0), (50, 5, 0, 0)]


def test_streaming_top_k_ties_keep_earliest_arrival():
    # rows tied at the k-th key: the earlier arrivals win, output arrival-ascending
    rows = [(5, 0, 0, 0), (5, 1, 0, 0), (5, 2, 0, 0), (5, 3, 0, 0)]
    assert streaming_top_k(rows, k=2) == [(5, 0, 0, 0), (5, 1, 0, 0)]
    # a strictly-larger latecomer still displaces a tied kept row
    mixed = [(5, 0, 0, 0), (5, 1, 0, 0), (9, 2, 0, 0)]
    assert streaming_top_k(mixed, k=2) == [(9, 2, 0, 0), (5, 0, 0, 0)]


def test_streaming_top_k_signed_key_comparison():
    # keys compared as signed int32: -1 (0xFFFFFFFF) is below 1, above -2**31
    rows = [(-1, 0, 0, 0), (1, 1, 0, 0), (-(2**31), 2, 0, 0), (2**31 - 1, 3, 0, 0)]
    assert streaming_top_k(rows, k=2) == [(2**31 - 1, 3, 0, 0), (1, 1, 0, 0)]
    assert streaming_top_k(rows, k=4)[-1] == (-(2**31), 2, 0, 0)


def test_streaming_top_k_k_at_or_over_row_count_returns_all_sorted():
    rows = [(30, 1, 0, 0), (90, 2, 0, 0), (10, 3, 0, 0)]
    full = [(90, 2, 0, 0), (30, 1, 0, 0), (10, 3, 0, 0)]
    assert streaming_top_k(rows, k=3) == full
    assert streaming_top_k(rows, k=100) == full
    assert streaming_top_k([], k=5) == []


def test_streaming_top_k_rejects_out_of_range_k():
    rows = [(1, 0, 0, 0)]
    for bad in (0, -1, TOP_K_MAX + 1):
        with pytest.raises(ValueError):
            streaming_top_k(rows, k=bad)
    assert TopKErrorCode.BAD_K == 1
    assert streaming_top_k(rows * TOP_K_MAX, k=TOP_K_MAX)  # boundary k is accepted


def test_streaming_top_k_matches_bounded_heap_model():
    import random

    rng = random.Random(23)
    for _ in range(400):
        n = rng.randint(0, 40)
        rows = [(rng.randint(-50, 50), i, rng.randint(0, 9), rng.randint(0, 9)) for i in range(n)]
        for k in (1, 2, 5, 13, TOP_K_MAX):
            assert streaming_top_k(rows, k=k) == _heap_top_k(rows, k)


def test_keyed_state_carries_a_dependency_per_key() -> None:
    # the property no preceding tile has: a row's output depends on earlier
    # rows with the SAME key, and is independent of rows with other keys
    rows = [(1, 5), (2, 3), (1, 4), (1, -2), (2, 10)]

    emitted, code = keyed_state_scan(rows, op=KeyedStateOp.ADD, capacity=8)

    assert code is KeyedStateErrorCode.OK
    assert emitted == [(1, 5), (2, 3), (1, 9), (1, 7), (2, 13)]
    # interleaving other keys between two rows of key 1 changes nothing
    interleaved = [(1, 5), (9, 100), (9, 100), (1, 4)]
    assert keyed_state_scan(interleaved, op=KeyedStateOp.ADD, capacity=8)[0][-1] == (1, 9)


def test_keyed_state_emits_post_update_so_back_to_back_rows_see_the_write() -> None:
    # the read-modify-write hazard the hardware must FORWARD rather than stall:
    # the second row of a key observes the first row's write, not the old value
    emitted, _ = keyed_state_scan([(7, 1), (7, 1), (7, 1)], op=KeyedStateOp.ADD, capacity=4)
    assert emitted == [(7, 1), (7, 2), (7, 3)]


def test_keyed_state_first_row_of_a_key_starts_from_initial() -> None:
    # a fresh key applies the op to `initial`, it does not adopt the value --
    # which only coincides for SET, hence `initial` being explicit
    assert keyed_state_scan([(1, 5)], op=KeyedStateOp.ADD, capacity=4, initial=100)[0] == [(1, 105)]
    assert keyed_state_scan([(1, 5)], op=KeyedStateOp.SET, capacity=4, initial=100)[0] == [(1, 5)]
    assert keyed_state_scan([(1, 5)], op=KeyedStateOp.MAX, capacity=4, initial=100)[0] == [(1, 100)]
    assert keyed_state_scan([(1, 5)], op=KeyedStateOp.MIN, capacity=4, initial=100)[0] == [(1, 5)]


def test_keyed_state_add_wraps_in_the_int32_domain() -> None:
    emitted, code = keyed_state_scan([(1, 2**31 - 1), (1, 1)], op=KeyedStateOp.ADD, capacity=4)
    assert code is KeyedStateErrorCode.OK
    assert emitted[-1] == (1, -(2**31))  # the encoded-domain convention, not a Python bigint


def test_keyed_state_refuses_to_evict_and_reports_where_it_filled() -> None:
    # capacity is a BRAM depth, so overflow is an error, not an eviction:
    # silently dropping a key makes the answer wrong undetectably. Rows already
    # emitted stay emitted so a caller can see exactly where the table filled.
    emitted, code = keyed_state_scan([(1, 5), (2, 3), (3, 1)], op=KeyedStateOp.ADD, capacity=2)

    assert code is KeyedStateErrorCode.KEY_CAPACITY_EXCEEDED
    assert emitted == [(1, 5), (2, 3)]
    # a re-used key does NOT consume capacity
    reused, ok = keyed_state_scan([(1, 5), (2, 3), (1, 4), (2, 1)], op=KeyedStateOp.ADD, capacity=2)
    assert ok is KeyedStateErrorCode.OK
    assert reused == [(1, 5), (2, 3), (1, 9), (2, 4)]


def test_keyed_state_rejects_a_capacity_the_hardware_cannot_hold() -> None:
    for bad in (0, -1, KEYED_STATE_MAX_KEYS + 1):
        emitted, code = keyed_state_scan([(1, 1)], op=KeyedStateOp.ADD, capacity=bad)
        assert (emitted, code) == ([], KeyedStateErrorCode.BAD_CAPACITY)


def test_keyed_state_keys_and_values_live_in_the_int32_domain() -> None:
    # keys and values are int32 WIRES, not Python integers. Getting this wrong
    # made the golden unimplementable rather than merely imprecise.
    same_key, code = keyed_state_scan([(-1, 1), (0xFFFFFFFF, 1)], op=KeyedStateOp.ADD, capacity=1)
    assert code is KeyedStateErrorCode.OK  # ONE key on a 32-bit bus, not two
    assert same_key == [(-1, 1), (-1, 2)]

    # a state a 32-bit port cannot carry is not a state the tile can be in
    assert keyed_state_scan([(1, 2**31)], op=KeyedStateOp.SET, capacity=1)[0] == [(1, -(2**31))]
    assert keyed_state_scan([(1, 2**31)], op=KeyedStateOp.MAX, capacity=1)[0] == [(1, 0)]
    assert keyed_state_scan([(1, 1)], op=KeyedStateOp.ADD, capacity=1, initial=2**31)[0] == [(1, -(2**31) + 1)]


def test_keyed_state_rejects_an_unsupported_op() -> None:
    # every non-SET/ADD/MAX value previously fell through to MIN
    with pytest.raises(ValueError, match="unsupported keyed state op"):
        keyed_state_scan([(1, 1)], op=99, capacity=4)  # type: ignore[arg-type]


def test_scratchpad_emits_post_op_state_per_row() -> None:
    rows = [(1, 0, 7), (0, 0, 999), (1, 1, -2), (0, 1, 0), (0, 2, 0)]
    emitted, code = scratchpad_scan(rows, granted_words=4)
    assert code is ScratchpadErrorCode.OK
    # write emits the written value; read emits stored; unwritten reads 0
    assert emitted == [(0, 7), (0, 7), (1, -2), (1, -2), (2, 0)]


def test_scratchpad_overflow_is_an_error_that_preserves_emitted_rows() -> None:
    emitted, code = scratchpad_scan([(1, 0, 5), (1, 4, 1)], granted_words=4)
    assert code is ScratchpadErrorCode.STORAGE_OVERFLOW  # addr 4 >= grant 4
    assert emitted == [(0, 5)]  # prefix preserved
    # negative addresses are int32 wires and never inside any grant
    _, code = scratchpad_scan([(1, -1, 5)], granted_words=4)
    assert code is ScratchpadErrorCode.STORAGE_OVERFLOW


def test_scratchpad_values_live_in_the_int32_domain() -> None:
    emitted, code = scratchpad_scan([(1, 0, 2**31)], granted_words=4)
    assert code is ScratchpadErrorCode.OK
    assert emitted == [(0, -(2**31))]  # wraps, per the wire


def test_scratchpad_rejects_a_grant_the_arena_cannot_hold() -> None:
    for bad in (0, -1, SCRATCHPAD_MAX_WORDS + 1):
        emitted, code = scratchpad_scan([(1, 0, 1)], granted_words=bad)
        assert (emitted, code) == ([], ScratchpadErrorCode.BAD_GRANT)

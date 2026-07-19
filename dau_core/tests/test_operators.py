from __future__ import annotations

import pytest

from dau_core.operators import (
    BarAggregate,
    BroadcastJoinConfig,
    FieldSumAggregate,
    FilterPredicate,
    GroupAggregate,
    GroupedFieldSumAggregate,
    JoinSource,
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
    bar_aggregate_rows,
    broadcast_hash_join_rows,
    field_sum_rows,
    filter_rows,
    filter_rows_by_membership,
    filter_rows_by_predicate,
    filter_values,
    grouped_aggregate_rows,
    grouped_field_sum_rows,
    map_row_fields,
    map_rows,
    map_values,
    pack_splitters,
)

_VALUES = [7, -2, 5, 11, 5, 0, -100, 2**31 - 1, -(2**31)]


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
    assert filter_values(_VALUES, FilterPredicate(op, constant)) == expected


def test_and_combine_is_range_filter() -> None:
    predicate = FilterPredicate(PredicateOp.GE, 0, PredicateCombine.AND, PredicateOp.LT, 10)
    assert filter_values(_VALUES, predicate) == [7, 5, 5, 0]


def test_or_combine_is_union() -> None:
    predicate = FilterPredicate(PredicateOp.LT, -50, PredicateCombine.OR, PredicateOp.GT, 50)
    assert filter_values(_VALUES, predicate) == [-100, 2**31 - 1, -(2**31)]


def test_empty_selection_and_empty_input() -> None:
    predicate = FilterPredicate(PredicateOp.GT, 2**31 - 1)
    assert filter_values(_VALUES, predicate) == []
    assert filter_values([], FilterPredicate(PredicateOp.EQ, 0)) == []


def test_order_is_preserved() -> None:
    predicate = FilterPredicate(PredicateOp.NE, 0)
    result = filter_values(_VALUES, predicate)
    assert result == [value for value in _VALUES if value != 0]


def test_constants_must_fit_int32() -> None:
    with pytest.raises(ValueError, match="int32"):
        FilterPredicate(PredicateOp.EQ, 2**31)


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
    predicate = FilterPredicate(PredicateOp.GT, 0)
    assert filter_rows(rows, predicate) == [(570, 100), (571, 99)]


def test_filter_rows_on_key_lane() -> None:
    rows = [(570, 100), (571, 99), (572, 5)]
    predicate = FilterPredicate(PredicateOp.LE, 571)
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
    eligible = filter_rows(trades, FilterPredicate(PredicateOp.GT, 0))
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
    assert filter_rows_by_predicate(_QUAD_ROWS, RowPredicateConfig(term0=RowPredicateTerm(RowField.KEY, PredicateOp.EQ, 568))) == [
        (568, 1000, 5, 10),
        (568, 1500, 7, 23),
        (568, 3000, 4, 1),
    ]
    assert filter_rows_by_predicate(_QUAD_ROWS, RowPredicateConfig(term0=RowPredicateTerm(RowField.VALUE, PredicateOp.LT, 0))) == [(570, -500, 6, 24)]
    assert filter_rows_by_predicate(_QUAD_ROWS, RowPredicateConfig(term0=RowPredicateTerm(RowField.AUX0, PredicateOp.GE, 7))) == [(568, 1500, 7, 23)]
    assert filter_rows_by_predicate(_QUAD_ROWS, RowPredicateConfig(term0=RowPredicateTerm(RowField.AUX1, PredicateOp.NE, 30))) == [
        row for row in _QUAD_ROWS if row[3] != 30
    ]


def test_row_predicate_three_term_tree_is_the_q6_where() -> None:
    # shipdate bucket EQ AND discount GE lo AND quantity LT 24
    config = RowPredicateConfig(
        term0=RowPredicateTerm(RowField.KEY, PredicateOp.EQ, 568),
        combine01=PredicateCombine.AND,
        term1=RowPredicateTerm(RowField.AUX0, PredicateOp.GE, 5),
        combine2=PredicateCombine.AND,
        term2=RowPredicateTerm(RowField.AUX1, PredicateOp.LT, 24),
    )
    assert filter_rows_by_predicate(_QUAD_ROWS, config) == [(568, 1000, 5, 10), (568, 1500, 7, 23)]


def test_row_predicate_or_of_and_levels() -> None:
    # (value > 2000 OR key == 570) AND aux1 < 30
    config = RowPredicateConfig(
        term0=RowPredicateTerm(RowField.VALUE, PredicateOp.GT, 2000),
        combine01=PredicateCombine.OR,
        term1=RowPredicateTerm(RowField.KEY, PredicateOp.EQ, 570),
        combine2=PredicateCombine.AND,
        term2=RowPredicateTerm(RowField.AUX1, PredicateOp.LT, 30),
    )
    assert filter_rows_by_predicate(_QUAD_ROWS, config) == [(570, -500, 6, 24), (568, 3000, 4, 1)]


def test_row_predicate_validation() -> None:
    with pytest.raises(ValueError, match="int32"):
        RowPredicateTerm(RowField.KEY, PredicateOp.EQ, 2**31)
    with pytest.raises(ValueError, match="term1"):
        RowPredicateConfig(term0=RowPredicateTerm(RowField.KEY, PredicateOp.EQ, 0), combine01=PredicateCombine.AND)
    with pytest.raises(ValueError, match="term2"):
        RowPredicateConfig(term0=RowPredicateTerm(RowField.KEY, PredicateOp.EQ, 0), combine2=PredicateCombine.OR)


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
_CARRY_BUILD = BroadcastJoinConfig(JoinSource.B0, JoinSource.B1, JoinSource.B2)


def test_broadcast_join_inner_join_carries_build_payload() -> None:
    out = broadcast_hash_join_rows(_JOIN_BUILD, _JOIN_PROBE, out_sel=_CARRY_BUILD, key_space=1024)
    # only the three hits emit, in probe order, carrying the build payload
    assert out == [(7, 100, 101, 102), (63, 300, 301, 302), (100, 200, 201, 202)]


def test_broadcast_join_field_select_mixes_build_and_probe() -> None:
    # q3 shape: (key, b0, v0, b2)
    cfg = BroadcastJoinConfig(JoinSource.B0, JoinSource.V0, JoinSource.B2)
    out = broadcast_hash_join_rows(_JOIN_BUILD, _JOIN_PROBE, out_sel=cfg, key_space=1024)
    assert out == [(7, 100, 1000, 102), (63, 300, 3000, 302), (100, 200, 4000, 202)]


def test_broadcast_join_carry_probe_columns() -> None:
    cfg = BroadcastJoinConfig(JoinSource.V0, JoinSource.V1, JoinSource.V2)
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
    with pytest.raises(ValueError, match="in \\[0, 5\\]"):
        BroadcastJoinConfig(6, JoinSource.B1, JoinSource.B2)
    with pytest.raises(ValueError, match="in \\[0, 5\\]"):
        BroadcastJoinConfig(JoinSource.B0, JoinSource.B1, 7)


def test_broadcast_join_bounded_key_space_and_order_preserved() -> None:
    build = [(0, 1, 2, 3), (63, 4, 5, 6)]
    probe = [(63, 0, 0, 0), (0, 0, 0, 0)]  # reversed order preserved on the probe side
    out = broadcast_hash_join_rows(build, probe, out_sel=_CARRY_BUILD, key_space=64)
    assert out == [(63, 4, 5, 6), (0, 1, 2, 3)]

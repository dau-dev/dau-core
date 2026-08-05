"""Hash-mode grouped field-sum aggregation golden: one accumulator per
DISTINCT key regardless of input order (the ordered tile's
``sorted_by_key`` staging precondition removed), key-ascending drain,
explicit overflow, and the host split-and-merge algebra that makes
overflow recoverable."""

import random

import pytest

from dau_core.operators import (
    HASH_GROUPED_MAX_GROUPS,
    HashGroupedErrorCode,
    grouped_field_sum_rows,
    hash_grouped_field_sum_rows,
    merge_hash_grouped,
)

_MASK = 0b0010  # sum the VALUE field


def _rows(seed: int, count: int, keys: int) -> list[tuple[int, int, int, int]]:
    rng = random.Random(seed)
    return [(rng.randrange(-keys // 2, keys // 2 + 1), rng.randint(-1000, 1000), 0, 0) for _ in range(count)]


def _as_pairs(groups):
    return [(group.key, group.sums, group.count) for group in groups]


def test_one_record_per_distinct_key_regardless_of_order() -> None:
    rows = [(5, 1, 0, 0), (-3, 2, 0, 0), (5, 10, 0, 0), (7, 4, 0, 0)]
    groups, error = hash_grouped_field_sum_rows(rows, field_mask=_MASK, capacity=8)
    assert error is HashGroupedErrorCode.OK
    assert _as_pairs(groups) == [(-3, (2,), 1), (5, (11,), 2), (7, (4,), 1)]
    # the ORDERED tile opens a second partial for the reappearing key 5 —
    # the difference this tile exists to remove
    ordered = grouped_field_sum_rows(rows, field_mask=_MASK)
    assert [group.key for group in ordered] == [5, -3, 5, 7]


def test_permutation_invariance() -> None:
    rows = _rows(seed=7, count=400, keys=40)
    reference, error = hash_grouped_field_sum_rows(rows, field_mask=_MASK, capacity=64)
    assert error is HashGroupedErrorCode.OK
    rng = random.Random(11)
    for _ in range(8):
        shuffled = rows[:]
        rng.shuffle(shuffled)
        groups, code = hash_grouped_field_sum_rows(shuffled, field_mask=_MASK, capacity=64)
        assert code is HashGroupedErrorCode.OK
        assert _as_pairs(groups) == _as_pairs(reference)


def test_sorted_input_agrees_with_the_ordered_tile() -> None:
    # the hash tile subsumes the ordered tile's answer when the input
    # already satisfies the ordered tile's precondition
    rows = sorted(_rows(seed=3, count=300, keys=25), key=lambda row: row[0])
    hashed, error = hash_grouped_field_sum_rows(rows, field_mask=0b1110, capacity=64)
    assert error is HashGroupedErrorCode.OK
    ordered = grouped_field_sum_rows(rows, field_mask=0b1110)
    assert _as_pairs(hashed) == _as_pairs(ordered)


def test_drain_is_signed_key_ascending() -> None:
    rows = [(key, 1, 0, 0) for key in (9, -9, 0, 2147483647, -2147483648, 3)]
    groups, error = hash_grouped_field_sum_rows(rows, field_mask=_MASK, capacity=16)
    assert error is HashGroupedErrorCode.OK
    assert [group.key for group in groups] == [-2147483648, -9, 0, 3, 9, 2147483647]


def test_keys_are_int32_wires() -> None:
    # -1 and 0xFFFFFFFF are the same key on a 32-bit bus
    groups, error = hash_grouped_field_sum_rows([(-1, 5, 0, 0), (0xFFFFFFFF, 7, 0, 0)], field_mask=_MASK, capacity=4)
    assert error is HashGroupedErrorCode.OK
    assert _as_pairs(groups) == [(-1, (12,), 2)]


def test_overflow_reports_and_keeps_the_accumulated_groups() -> None:
    rows = [(1, 10, 0, 0), (2, 20, 0, 0), (1, 5, 0, 0), (3, 30, 0, 0), (2, 1, 0, 0)]
    groups, error = hash_grouped_field_sum_rows(rows, field_mask=_MASK, capacity=2)
    assert error is HashGroupedErrorCode.GROUP_CAPACITY_EXCEEDED
    # keys 1 and 2 accumulated everything BEFORE the overflowing row; the
    # tile never evicts, and rows after the overflow are not counted
    assert _as_pairs(groups) == [(1, (15,), 2), (2, (20,), 1)]


def test_full_table_without_a_new_key_is_not_an_overflow() -> None:
    rows = [(1, 1, 0, 0), (2, 2, 0, 0), (1, 3, 0, 0), (2, 4, 0, 0)]
    groups, error = hash_grouped_field_sum_rows(rows, field_mask=_MASK, capacity=2)
    assert error is HashGroupedErrorCode.OK
    assert _as_pairs(groups) == [(1, (4,), 2), (2, (6,), 2)]


def test_bad_configuration_is_refused() -> None:
    rows = [(1, 1, 0, 0)]
    assert hash_grouped_field_sum_rows(rows, field_mask=_MASK, capacity=0)[1] is HashGroupedErrorCode.BAD_CAPACITY
    assert hash_grouped_field_sum_rows(rows, field_mask=_MASK, capacity=HASH_GROUPED_MAX_GROUPS + 1)[1] is HashGroupedErrorCode.BAD_CAPACITY
    assert hash_grouped_field_sum_rows(rows, field_mask=0, capacity=4)[1] is HashGroupedErrorCode.BAD_FIELD_MASK
    assert hash_grouped_field_sum_rows(rows, field_mask=16, capacity=4)[1] is HashGroupedErrorCode.BAD_FIELD_MASK
    # capacity is checked before the mask: both invalid reports capacity
    assert hash_grouped_field_sum_rows(rows, field_mask=0, capacity=0)[1] is HashGroupedErrorCode.BAD_CAPACITY


def test_split_and_merge_equals_one_shot() -> None:
    """The property that makes overflow recoverable rather than fatal."""
    rows = _rows(seed=5, count=600, keys=50)
    one_shot, error = hash_grouped_field_sum_rows(rows, field_mask=0b1110, capacity=64)
    assert error is HashGroupedErrorCode.OK
    for chunks in (2, 3, 7, 13):
        size = (len(rows) + chunks - 1) // chunks
        partials = []
        for start in range(0, len(rows), size):
            partial, code = hash_grouped_field_sum_rows(rows[start : start + size], field_mask=0b1110, capacity=64)
            assert code is HashGroupedErrorCode.OK
            partials.append(partial)
        assert _as_pairs(merge_hash_grouped(partials)) == _as_pairs(one_shot), chunks


def test_key_range_split_recovers_an_overflowed_batch() -> None:
    # the host's actual response to GROUP_CAPACITY_EXCEEDED: split by key
    # range so each pass fits the table, then merge
    rows = _rows(seed=17, count=400, keys=40)
    _, error = hash_grouped_field_sum_rows(rows, field_mask=_MASK, capacity=8)
    assert error is HashGroupedErrorCode.GROUP_CAPACITY_EXCEEDED
    partials = []
    for low, high in ((-21, -7), (-7, 7), (7, 21)):
        subset = [row for row in rows if low <= row[0] < high]
        partial, code = hash_grouped_field_sum_rows(subset, field_mask=_MASK, capacity=16)
        assert code is HashGroupedErrorCode.OK
        partials.append(partial)
    merged = merge_hash_grouped(partials)
    reference, code = hash_grouped_field_sum_rows(rows, field_mask=_MASK, capacity=64)
    assert code is HashGroupedErrorCode.OK
    assert _as_pairs(merged) == _as_pairs(reference)


def test_merging_mixed_field_masks_is_a_caller_error() -> None:
    rows = _rows(seed=2, count=20, keys=5)
    a, _ = hash_grouped_field_sum_rows(rows, field_mask=0b0010, capacity=16)
    b, _ = hash_grouped_field_sum_rows(rows, field_mask=0b0100, capacity=16)
    with pytest.raises(ValueError, match="different field masks"):
        merge_hash_grouped([a, b])


def test_empty_batch_emits_nothing() -> None:
    groups, error = hash_grouped_field_sum_rows([], field_mask=_MASK, capacity=8)
    assert error is HashGroupedErrorCode.OK and groups == []
    assert merge_hash_grouped([]) == []


def test_record_words_match_the_ordered_tile_shape() -> None:
    groups, _ = hash_grouped_field_sum_rows([(-2, -5, 0, 0)], field_mask=_MASK, capacity=4)
    ordered = grouped_field_sum_rows([(-2, -5, 0, 0)], field_mask=_MASK)
    assert groups[0].record_words() == ordered[0].record_words()

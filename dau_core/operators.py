from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum

from .stream import LogicalType, OperationCode

FILTER_CONFIG_VERSION = "0.1"


class PredicateOp(IntEnum):
    """Comparison opcodes for the predicate filter tile. Zero is reserved so an
    unconfigured tile fails explicitly."""

    EQ = 1
    NE = 2
    LT = 3
    LE = 4
    GT = 5
    GE = 6


class PredicateCombine(IntEnum):
    SINGLE = 0
    AND = 1
    OR = 2


class FilterErrorCode(IntEnum):
    CONFIG = 1
    STREAM = 2


@dataclass(frozen=True)
class FilterPredicate:
    """Predicate configuration: ``value <op0> constant0`` optionally combined
    with ``value <op1> constant1`` by AND/OR. Matches the tile's config ports."""

    op0: PredicateOp
    constant0: int
    combine: PredicateCombine = PredicateCombine.SINGLE
    op1: PredicateOp = PredicateOp.EQ
    constant1: int = 0

    def __post_init__(self) -> None:
        for name in ("constant0", "constant1"):
            value = getattr(self, name)
            if not -(1 << 31) <= value <= (1 << 31) - 1:
                raise ValueError(f"{name} must fit in int32")

    def matches(self, value: int) -> bool:
        first = _compare(self.op0, value, self.constant0)
        if self.combine == PredicateCombine.SINGLE:
            return first
        second = _compare(self.op1, value, self.constant1)
        return (first and second) if self.combine == PredicateCombine.AND else (first or second)


def filter_values(values: Iterable[int], predicate: FilterPredicate) -> list[int]:
    """Golden semantics for the compacting predicate filter tile: emit the
    matching values in input order."""
    return [value for value in values if predicate.matches(value)]


def _compare(op: PredicateOp, value: int, constant: int) -> bool:
    if op == PredicateOp.EQ:
        return value == constant
    if op == PredicateOp.NE:
        return value != constant
    if op == PredicateOp.LT:
        return value < constant
    if op == PredicateOp.LE:
        return value <= constant
    if op == PredicateOp.GT:
        return value > constant
    if op == PredicateOp.GE:
        return value >= constant
    raise ValueError(f"unsupported predicate op {op!r}")


MAP_CONFIG_VERSION = "0.1"

_INT32_MASK = 0xFFFF_FFFF


class MapOp(IntEnum):
    """ALU opcodes for the map tile. Zero is reserved so an unconfigured tile
    fails explicitly."""

    ADD = 1
    SUB = 2
    MUL = 3
    MULSHR = 4  # (value * constant) >> shift: multiply-shift floor division / bucket derivation
    SHL = 5
    AND = 6
    OR = 7


class MapErrorCode(IntEnum):
    CONFIG = 1
    STREAM = 2


@dataclass(frozen=True)
class MapConfig:
    """Per-row ALU configuration: ``value <op> constant`` (MULSHR/SHL also use
    ``shift``). All arithmetic is INT32 two's-complement wraparound — the
    golden and the tile agree bit-for-bit. Matches the tile's config ports."""

    op: MapOp
    constant: int = 0
    shift: int = 0

    def __post_init__(self) -> None:
        if not -(1 << 31) <= self.constant <= (1 << 31) - 1:
            raise ValueError("constant must fit in int32")
        if not 0 <= self.shift <= 31:
            raise ValueError("shift must be in [0, 31]")

    def apply(self, value: int) -> int:
        if self.op == MapOp.ADD:
            return _wrap_int32(value + self.constant)
        if self.op == MapOp.SUB:
            return _wrap_int32(value - self.constant)
        if self.op == MapOp.MUL:
            return _wrap_int32(value * self.constant)
        if self.op == MapOp.MULSHR:
            # 64-bit signed product, arithmetic shift right
            return _wrap_int32((value * self.constant) >> self.shift)
        if self.op == MapOp.SHL:
            return _wrap_int32(value << self.shift)
        if self.op == MapOp.AND:
            return _wrap_int32((value & _INT32_MASK) & (self.constant & _INT32_MASK))
        if self.op == MapOp.OR:
            return _wrap_int32((value & _INT32_MASK) | (self.constant & _INT32_MASK))
        raise ValueError(f"unsupported map op {self.op!r}")


def map_values(values: Iterable[int], config: MapConfig) -> list[int]:
    """Golden semantics for the map/ALU tile: apply the configured ALU
    operation to every value, preserving order and count."""
    return [config.apply(value) for value in values]


def _wrap_int32(value: int) -> int:
    value &= _INT32_MASK
    return value - (1 << 32) if value >= (1 << 31) else value


def filter_rows(rows: Iterable[tuple[int, int]], predicate: FilterPredicate, *, on_key: bool = False) -> list[tuple[int, int]]:
    """Golden semantics for the keyed predicate-filter tile: the predicate is
    evaluated on the value lane (or the key lane with ``on_key``) and whole
    ``(key, value)`` rows pass or drop together — compaction never misaligns
    the lanes."""
    return [(key, value) for key, value in rows if predicate.matches(key if on_key else value)]


def map_rows(rows: Iterable[tuple[int, int]], config: MapConfig, *, on_key: bool = False) -> list[tuple[int, int]]:
    """Golden semantics for the keyed map/ALU tile: the ALU operation applies
    to the value lane (or the key lane with ``on_key`` — e.g. time→bucket
    derivation); the other lane passes through unchanged, order and count
    preserved."""
    if on_key:
        return [(config.apply(key), value) for key, value in rows]
    return [(key, config.apply(value)) for key, value in rows]


GROUPED_AGGREGATION_CONFIG_VERSION = "0.1"


class GroupedAggregationErrorCode(IntEnum):
    CONFIG = 1
    STREAM = 2


@dataclass(frozen=True)
class GroupAggregate:
    """One closed group from the ordered grouped-aggregation tile: every
    statistic is computed in a single pass. ``first``/``last`` follow input
    order, so partial groups from separate batches merge correctly on the
    host (the partial-bar contract)."""

    key: int
    first: int
    last: int
    minimum: int
    maximum: int
    total: int
    count: int


@dataclass(frozen=True)
class BarAggregate:
    """One closed bar from the fused single-pass bar-aggregation tile: price
    OHLC statistics plus the exact micro-share volume decomposition, all in
    one pass over (key, price, shares, frac) rows."""

    key: int
    open: int
    close: int
    low: int
    high: int
    shares: int
    frac: int
    count: int


def filter_bar_rows_by_key(rows: Iterable[tuple[int, int, int, int]], *, key_mask: int, key_match: int) -> list[tuple[int, int, int, int]]:
    """Golden semantics for the pair-row key partition filter: whole
    (key, price, shares, frac) rows pass when ``(key & mask) == match``
    (32-bit two's-complement key lane), order preserved."""
    return [row for row in rows if ((row[0] & 0xFFFF_FFFF) & (key_mask & 0xFFFF_FFFF)) == (key_match & 0xFFFF_FFFF)]


def range_partition_bar_rows(rows: Iterable[tuple[int, int, int, int]], *, splitters: Iterable[int]) -> list[list[tuple[int, int, int, int]]]:
    """Golden semantics for the range partitioner: route each
    (key, price, shares, frac) row to partition
    ``p = #{ s in splitters : signed32(key) >= signed32(s) }`` (splitters
    monotonic non-decreasing, signed 32-bit key lane), order preserved within
    each partition. Returns one ordered list per partition
    (length ``len(splitters) + 1``)."""
    bounds = [_wrap_int32(s) for s in splitters]
    parts: list[list[tuple[int, int, int, int]]] = [[] for _ in range(len(bounds) + 1)]
    for row in rows:
        key = _wrap_int32(row[0])
        parts[sum(1 for bound in bounds if key >= bound)].append(tuple(row))
    return parts


class SorterErrorCode(IntEnum):
    STREAM = 2
    CAPACITY = 3


def sort_bar_rows_by_key(rows: Iterable[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Golden semantics for the batch bitonic sorter: rows ascending by the
    signed 32-bit key, STABLE (rows with equal keys keep input order — the
    tile tie-breaks on load index)."""
    return sorted((tuple(row) for row in rows), key=lambda row: _wrap_int32(row[0]))


def bar_aggregate_rows(rows: Iterable[tuple[int, int, int, int]]) -> list[BarAggregate]:
    """Golden semantics for the fused bar-aggregation tile (ordered
    close-out): consecutive rows with equal keys form a bar; volume is
    ``shares * SCALE + frac`` recombined on the host."""
    results: list[BarAggregate] = []
    current: BarAggregate | None = None
    for key, price, shares, frac in rows:
        if current is None or key != current.key:
            if current is not None:
                results.append(current)
            current = BarAggregate(key=key, open=price, close=price, low=price, high=price, shares=shares, frac=frac, count=1)
        else:
            current = BarAggregate(
                key=key,
                open=current.open,
                close=price,
                low=min(current.low, price),
                high=max(current.high, price),
                shares=current.shares + shares,
                frac=current.frac + frac,
                count=current.count + 1,
            )
    if current is not None:
        results.append(current)
    return results


def grouped_aggregate_rows(rows: Iterable[tuple[int, int]]) -> list[GroupAggregate]:
    """Golden semantics for the ordered grouped-aggregation tile
    (close-out mode): consecutive rows with equal keys form a group; a group
    closes when the key changes or input ends. A key that reappears later
    opens a NEW group — partials are merged by the host, never the tile."""
    results: list[GroupAggregate] = []
    current: GroupAggregate | None = None
    for key, value in rows:
        if current is None or key != current.key:
            if current is not None:
                results.append(current)
            current = GroupAggregate(key=key, first=value, last=value, minimum=value, maximum=value, total=value, count=1)
        else:
            current = GroupAggregate(
                key=key,
                first=current.first,
                last=value,
                minimum=min(current.minimum, value),
                maximum=max(current.maximum, value),
                total=current.total + value,
                count=current.count + 1,
            )
    if current is not None:
        results.append(current)
    return results


class EmptyInputError(ValueError):
    pass


class UnsupportedAggregationError(ValueError):
    pass


_INTEGER_RANGES = {
    LogicalType.INT8: (-(1 << 7), (1 << 7) - 1),
    LogicalType.INT16: (-(1 << 15), (1 << 15) - 1),
    LogicalType.INT32: (-(1 << 31), (1 << 31) - 1),
    LogicalType.INT64: (-(1 << 63), (1 << 63) - 1),
    LogicalType.UINT8: (0, (1 << 8) - 1),
    LogicalType.UINT16: (0, (1 << 16) - 1),
    LogicalType.UINT32: (0, (1 << 32) - 1),
    LogicalType.UINT64: (0, (1 << 64) - 1),
}

_FLOAT_TYPES = {LogicalType.FLOAT32, LogicalType.FLOAT64}


def aggregate_values(opcode: OperationCode, values: Iterable[int | float | bool], logical_type: LogicalType) -> int | float:
    materialized_values = list(values)

    if opcode == OperationCode.COUNT:
        return len(materialized_values)

    if not materialized_values:
        if opcode == OperationCode.SUM:
            return 0.0 if logical_type in _FLOAT_TYPES else 0
        raise EmptyInputError(f"{opcode.name.lower()} requires at least one value")

    if logical_type in _FLOAT_TYPES and any(_is_nan(value) for value in materialized_values):
        return math.nan

    if opcode == OperationCode.MIN:
        return min(materialized_values)
    if opcode == OperationCode.MAX:
        return max(materialized_values)
    if opcode == OperationCode.SUM:
        return _sum_values(materialized_values, logical_type)
    if opcode == OperationCode.AVERAGE:
        return float(sum(materialized_values) / len(materialized_values))

    raise UnsupportedAggregationError(f"unsupported operation {opcode!r}")


def synthetic_values(name: str, logical_type: LogicalType) -> list[int | float | bool]:
    if name == "empty":
        return []
    if name == "one":
        return [1.0] if logical_type in _FLOAT_TYPES else [1]
    if name == "odd_ints":
        return [-3, -1, 0, 1, 3]
    if name == "with_nan":
        if logical_type not in _FLOAT_TYPES:
            raise ValueError("with_nan fixture requires a floating-point logical type")
        return [1.0, math.nan, 3.0]
    if name == "repeated":
        return [2.0, 2.0, 2.0, 2.0] if logical_type in _FLOAT_TYPES else [2, 2, 2, 2]
    if name == "sorted":
        return [1.0, 2.0, 3.0, 4.0] if logical_type in _FLOAT_TYPES else [1, 2, 3, 4]
    if name == "skewed":
        return [0.0, 0.0, 0.0, 99.0] if logical_type in _FLOAT_TYPES else [0, 0, 0, 99]
    if name == "bounds":
        return _bounds_fixture(logical_type)
    raise ValueError(f"unknown synthetic fixture {name!r}")


def _sum_values(values: list[int | float | bool], logical_type: LogicalType) -> int | float:
    total = sum(values)
    if logical_type in _INTEGER_RANGES:
        minimum, maximum = _INTEGER_RANGES[logical_type]
        return min(max(int(total), minimum), maximum)
    if logical_type in _FLOAT_TYPES:
        return float(total)
    raise UnsupportedAggregationError(f"sum does not support {logical_type.name.lower()}")


def _bounds_fixture(logical_type: LogicalType) -> list[int | float | bool]:
    if logical_type in _INTEGER_RANGES:
        minimum, maximum = _INTEGER_RANGES[logical_type]
        return [minimum, 0, maximum]
    if logical_type in _FLOAT_TYPES:
        return [-math.inf, 0.0, math.inf]
    if logical_type == LogicalType.BOOL:
        return [False, True]
    raise ValueError(f"no bounds fixture for {logical_type.name.lower()}")


def _is_nan(value: int | float | bool) -> bool:
    return isinstance(value, float) and math.isnan(value)

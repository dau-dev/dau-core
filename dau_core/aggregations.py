from __future__ import annotations

import math
from collections.abc import Iterable

from .stream import LogicalType, OperationCode


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

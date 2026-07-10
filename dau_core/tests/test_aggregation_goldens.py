import math

import pytest

from dau_core.operators import EmptyInputError, aggregate_values, synthetic_values
from dau_core.stream import LogicalType, OperationCode


def test_integer_aggregations_use_saturating_sum_policy():
    assert aggregate_values(OperationCode.MIN, [7, -3, 2], LogicalType.INT32) == -3
    assert aggregate_values(OperationCode.MAX, [7, -3, 2], LogicalType.INT32) == 7
    assert aggregate_values(OperationCode.COUNT, [7, -3, 2], LogicalType.INT32) == 3
    assert aggregate_values(OperationCode.SUM, [2_147_483_647, 1], LogicalType.INT32) == 2_147_483_647
    assert aggregate_values(OperationCode.SUM, [-2_147_483_648, -1], LogicalType.INT32) == -2_147_483_648


def test_average_returns_float64_for_integer_and_float_inputs():
    assert aggregate_values(OperationCode.AVERAGE, [1, 2], LogicalType.INT32) == 1.5
    assert isinstance(aggregate_values(OperationCode.AVERAGE, [1.0, 2.0], LogicalType.FLOAT32), float)


def test_float_nan_policy_returns_nan_for_value_aggregations():
    values = [1.0, math.nan, 3.0]

    assert math.isnan(aggregate_values(OperationCode.MIN, values, LogicalType.FLOAT32))
    assert math.isnan(aggregate_values(OperationCode.MAX, values, LogicalType.FLOAT32))
    assert math.isnan(aggregate_values(OperationCode.SUM, values, LogicalType.FLOAT32))
    assert math.isnan(aggregate_values(OperationCode.AVERAGE, values, LogicalType.FLOAT32))
    assert aggregate_values(OperationCode.COUNT, values, LogicalType.FLOAT32) == 3


def test_empty_inputs_have_explicit_semantics():
    assert aggregate_values(OperationCode.COUNT, [], LogicalType.INT32) == 0
    assert aggregate_values(OperationCode.SUM, [], LogicalType.INT32) == 0
    with pytest.raises(EmptyInputError):
        aggregate_values(OperationCode.MIN, [], LogicalType.INT32)
    with pytest.raises(EmptyInputError):
        aggregate_values(OperationCode.AVERAGE, [], LogicalType.INT32)


def test_synthetic_fixture_values_cover_core_shapes():
    assert synthetic_values("odd_ints", LogicalType.INT32) == [-3, -1, 0, 1, 3]
    assert synthetic_values("with_nan", LogicalType.FLOAT32)[1] != synthetic_values("with_nan", LogicalType.FLOAT32)[1]

from .aggregations import EmptyInputError, UnsupportedAggregationError, aggregate_values, synthetic_values
from .capabilities import DeviceCapabilities, DmaConstraints, OperatorCapability
from .registers import (
    DAU_MAGIC_WORD,
    RegisterBlockSnapshot,
    RegisterMapVersion,
    RegisterOffset,
    RegisterWriteError,
    ResetScope,
    StreamProtocolVersion,
)
from .stream import (
    BatchHeader,
    ColumnDescriptor,
    GroupRole,
    InvalidStreamError,
    LogicalType,
    OperationCode,
    OperationDescriptor,
    PolicyFlag,
    StreamFlag,
)

__version__ = "0.1.0"

__all__ = [
    "BatchHeader",
    "ColumnDescriptor",
    "DAU_MAGIC_WORD",
    "DeviceCapabilities",
    "DmaConstraints",
    "EmptyInputError",
    "GroupRole",
    "InvalidStreamError",
    "LogicalType",
    "OperationCode",
    "OperationDescriptor",
    "OperatorCapability",
    "PolicyFlag",
    "RegisterMapVersion",
    "RegisterBlockSnapshot",
    "RegisterOffset",
    "RegisterWriteError",
    "ResetScope",
    "StreamFlag",
    "StreamProtocolVersion",
    "UnsupportedAggregationError",
    "aggregate_values",
    "synthetic_values",
]

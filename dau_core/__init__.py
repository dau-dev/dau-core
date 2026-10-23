"""dau-core: the tile palette, the device contracts and the goldens.

Exports load on first use (:mod:`dau_core._lazy`). Importing this package
must not import ccflow and its stack, because hydra's searchpath plugin
imports ``dau_core.config`` to discover what is installed, and discovery
wants none of these names.
"""

from typing import TYPE_CHECKING

from ._lazy import lazy_exports

if TYPE_CHECKING:
    from .capabilities import DeviceCapabilities, DmaConstraints, OperatorCapability
    from .capability_contract import (
        CAPABILITY_CONTRACT_SCHEMA,
        CapabilityContract,
        LaneCapability,
        TileCapability,
        capability_contract,
        opcode_bitmap,
        tile_capability,
    )
    from .design import DauDesign, OperatorUnit, UnitKind, format_units, parse_units
    from .operators import (
        EmptyInputError,
        UnsupportedAggregationError,
        aggregate_values,
        pack_splitters,
        synthetic_values,
    )
    from .registers import (
        CURRENT_REGISTER_MAP_VERSION,
        DAU_MAGIC_WORD,
        DEFAULT_STREAM_JOB_REGISTER_CONTRACT,
        JobControlFlag,
        JobStatusFlag,
        RegisterBlockSnapshot,
        RegisterMapVersion,
        RegisterOffset,
        RegisterWriteError,
        ResetScope,
        StreamJobRegisterContract,
        StreamProtocolVersion,
    )
    from .stream import (
        DEFAULT_RECORD_BATCH_LIMITS,
        BatchHeader,
        ColumnDescriptor,
        GroupRole,
        InvalidStreamError,
        LogicalType,
        OperationCode,
        OperationDescriptor,
        PolicyFlag,
        RecordBatchLimits,
        StreamAggregationErrorCode,
        StreamFlag,
        validate_record_batch,
    )

__version__ = "0.2.0"

# {submodule: the names it exports}. ``__all__`` and the lazy resolver both
# come from this one declaration, so a name cannot be in one and missing
# from the other; ``tests/test_lazy_exports.py`` checks it against the
# TYPE_CHECKING block above and resolves every name.
_EXPORTS = {
    "capabilities": ("DeviceCapabilities", "DmaConstraints", "OperatorCapability"),
    "capability_contract": (
        "CAPABILITY_CONTRACT_SCHEMA",
        "CapabilityContract",
        "LaneCapability",
        "TileCapability",
        "capability_contract",
        "opcode_bitmap",
        "tile_capability",
    ),
    "design": ("DauDesign", "OperatorUnit", "UnitKind", "format_units", "parse_units"),
    "operators": (
        "EmptyInputError",
        "UnsupportedAggregationError",
        "aggregate_values",
        "pack_splitters",
        "synthetic_values",
    ),
    "registers": (
        "CURRENT_REGISTER_MAP_VERSION",
        "DAU_MAGIC_WORD",
        "DEFAULT_STREAM_JOB_REGISTER_CONTRACT",
        "JobControlFlag",
        "JobStatusFlag",
        "RegisterBlockSnapshot",
        "RegisterMapVersion",
        "RegisterOffset",
        "RegisterWriteError",
        "ResetScope",
        "StreamJobRegisterContract",
        "StreamProtocolVersion",
    ),
    "stream": (
        "DEFAULT_RECORD_BATCH_LIMITS",
        "BatchHeader",
        "ColumnDescriptor",
        "GroupRole",
        "InvalidStreamError",
        "LogicalType",
        "OperationCode",
        "OperationDescriptor",
        "PolicyFlag",
        "RecordBatchLimits",
        "StreamAggregationErrorCode",
        "StreamFlag",
        "validate_record_batch",
    ),
}

__getattr__, __all__ = lazy_exports(__name__, _EXPORTS)

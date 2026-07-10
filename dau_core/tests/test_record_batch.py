from __future__ import annotations

import pytest

from dau_core.stream import (
    BatchHeader,
    ColumnDescriptor,
    InvalidStreamError,
    LogicalType,
    RecordBatchLimits,
    StreamFlag,
    validate_record_batch,
)


def _header(row_count: int, columns: int, payload_offset: int, payload_length: int) -> BatchHeader:
    return BatchHeader(
        schema_hash=0,
        batch_id=0,
        row_count=row_count,
        column_count=columns,
        operation_count=0,
        group_key_count=0,
        descriptor_table_offset=BatchHeader.SIZE_BYTES,
        payload_offset=payload_offset,
        payload_length=payload_length,
        flags=StreamFlag.NONE,
    )


def _column(field_id: int, offset: int, rows: int, width_bytes: int, logical_type: LogicalType = LogicalType.INT32) -> ColumnDescriptor:
    return ColumnDescriptor(
        field_id=field_id,
        logical_type=logical_type,
        physical_width_bits=width_bytes * 8,
        row_count=rows,
        null_count=0,
        payload_offset=offset,
        payload_length=rows * width_bytes,
    )


def test_valid_multi_column_batch_passes() -> None:
    header = _header(row_count=16, columns=3, payload_offset=256, payload_length=448)
    columns = (
        _column(0, 256, 16, 4),  # int32, 64 bytes
        _column(1, 320, 16, 8, LogicalType.INT64),  # 128 bytes
        _column(2, 448, 16, 4),  # 64 bytes, ends at 512 <= 256+448
    )
    validate_record_batch(header, columns)


def test_column_count_mismatch_rejected() -> None:
    header = _header(16, 2, 256, 192)
    with pytest.raises(InvalidStreamError, match="does not match"):
        validate_record_batch(header, (_column(0, 256, 16, 4),))


def test_column_cap_enforced() -> None:
    limits = RecordBatchLimits(max_columns=2)
    header = _header(16, 3, 256, 448)
    columns = (_column(0, 256, 16, 4), _column(1, 320, 16, 8, LogicalType.INT64), _column(2, 448, 16, 4))
    with pytest.raises(InvalidStreamError, match="exceeds limit 2"):
        validate_record_batch(header, columns, limits=limits)


def test_row_cap_enforced() -> None:
    limits = RecordBatchLimits(max_rows=8)
    header = _header(16, 1, 256, 64)
    with pytest.raises(InvalidStreamError, match="row_count 16 exceeds"):
        validate_record_batch(header, (_column(0, 256, 16, 4),), limits=limits)


def test_batch_extent_cap_enforced() -> None:
    limits = RecordBatchLimits(max_batch_bytes=256)
    header = _header(16, 1, 256, 64)
    with pytest.raises(InvalidStreamError, match="batch extent"):
        validate_record_batch(header, (_column(0, 256, 16, 4),), limits=limits)


def test_duplicate_field_ids_rejected() -> None:
    header = _header(16, 2, 256, 192)
    columns = (_column(7, 256, 16, 4), _column(7, 320, 16, 4))
    with pytest.raises(InvalidStreamError, match="unique"):
        validate_record_batch(header, columns)


def test_overlapping_column_payloads_rejected() -> None:
    header = _header(16, 2, 256, 192)
    columns = (_column(0, 256, 16, 8, LogicalType.INT64), _column(1, 320, 16, 4))  # first ends at 384 > 320
    with pytest.raises(InvalidStreamError, match="overlaps"):
        validate_record_batch(header, columns)


def test_misaligned_column_offset_rejected() -> None:
    header = _header(16, 1, 256, 192)
    column = ColumnDescriptor(
        field_id=0,
        logical_type=LogicalType.INT32,
        physical_width_bits=32,
        row_count=16,
        null_count=0,
        payload_offset=260,
        payload_length=64,
    )
    with pytest.raises(InvalidStreamError, match="aligned"):
        validate_record_batch(header, (column,))


def test_row_count_mismatch_rejected() -> None:
    header = _header(16, 1, 256, 64)
    with pytest.raises(InvalidStreamError, match="row_count does not match"):
        validate_record_batch(header, (_column(0, 256, 8, 4),))

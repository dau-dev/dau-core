from dau_core.stream import (
    BatchHeader,
    ColumnDescriptor,
    GroupRole,
    LogicalType,
    OperationCode,
    OperationDescriptor,
    PolicyFlag,
    StreamAggregationErrorCode,
)


def test_batch_header_packs_to_exactly_sixty_four_bytes():
    header = BatchHeader(
        schema_hash=0xAABBCCDD00112233,
        batch_id=7,
        row_count=1024,
        column_count=2,
        operation_count=1,
        group_key_count=1,
        descriptor_table_offset=64,
        payload_offset=256,
        payload_length=4096,
    )

    packed = header.pack()

    assert len(packed) == BatchHeader.SIZE_BYTES == 64
    assert BatchHeader.unpack(packed) == header


def test_column_descriptor_preserves_arrow_lite_offsets_and_group_role():
    descriptor = ColumnDescriptor(
        field_id=3,
        logical_type=LogicalType.INT32,
        physical_width_bits=32,
        row_count=1024,
        null_count=0,
        payload_offset=256,
        payload_length=4096,
        validity_offset=0,
        validity_length=0,
        group_role=GroupRole.KEY,
    )

    packed = descriptor.pack()

    assert len(packed) == ColumnDescriptor.SIZE_BYTES == 64
    assert ColumnDescriptor.unpack(packed) == descriptor


def test_operation_descriptor_records_float64_average_policy_and_group_key_set():
    descriptor = OperationDescriptor(
        operation_id=9,
        opcode=OperationCode.AVERAGE,
        input_column_id=3,
        output_field_id=11,
        output_type=LogicalType.FLOAT64,
        accumulator_width_bits=64,
        policy_flags=PolicyFlag.FLOAT64_AVERAGE,
        group_key_set_id=1,
    )

    packed = descriptor.pack()

    assert len(packed) == OperationDescriptor.SIZE_BYTES == 64
    assert OperationDescriptor.unpack(packed) == descriptor


def test_stream_aggregation_error_codes_match_hdl_contract_values():
    assert int(StreamAggregationErrorCode.DESCRIPTOR) == 1
    assert int(StreamAggregationErrorCode.OPCODE) == 2
    assert int(StreamAggregationErrorCode.TILE) == 3

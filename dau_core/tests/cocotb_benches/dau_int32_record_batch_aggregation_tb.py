from __future__ import annotations

import struct

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import aggregate_values
from dau_core.stream import (
    BatchHeader,
    ColumnDescriptor,
    GroupRole,
    LogicalType,
    OperationCode,
    OperationDescriptor,
    StreamAggregationErrorCode,
    StreamFlag,
)

_ALIGN = 64
_WIDTHS = {LogicalType.INT32: 4, LogicalType.INT64: 8}
_CODES = {LogicalType.INT32: "i", LogicalType.INT64: "q"}


@cocotb.test()
async def record_batch_aggregates_selected_middle_column(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    selected = [10, -3, 5, 8]
    columns = [
        (LogicalType.INT32, [1, 1, 1, 1]),
        (LogicalType.INT32, selected),
        (LogicalType.INT64, [7_000_000, 8_000_000, 9_000_000, 10_000_000]),
    ]
    await _reset(dut)
    await _send_words(dut, _words_from_bytes(_encode_batch(columns, OperationCode.SUM, input_column_id=1)))
    output_words = await _collect_output_words(dut)
    await _accept_success_status(dut)

    assert _result_payload(output_words) == int(aggregate_values(OperationCode.SUM, selected, LogicalType.INT32))
    assert _result_logical_type(output_words) == int(LogicalType.INT64)


@cocotb.test()
async def record_batch_first_column_min_with_trailing_columns_skipped(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    selected = [7, -2, 5, 11, 5]
    columns = [
        (LogicalType.INT32, selected),
        (LogicalType.INT64, [1, 2, 3, 4, 5]),
    ]
    await _reset(dut)
    await _send_words(dut, _words_from_bytes(_encode_batch(columns, OperationCode.MIN, input_column_id=0)))
    output_words = await _collect_output_words(dut)
    await _accept_success_status(dut)

    assert _result_payload(output_words, signed=True) == int(aggregate_values(OperationCode.MIN, selected, LogicalType.INT32))


@cocotb.test()
async def record_batch_count_outputs_uint64(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    columns = [(LogicalType.INT32, [4, 4, 4]), (LogicalType.INT32, [9, 9, 9])]
    await _reset(dut)
    await _send_words(dut, _words_from_bytes(_encode_batch(columns, OperationCode.COUNT, input_column_id=1)))
    output_words = await _collect_output_words(dut)
    await _accept_success_status(dut)

    assert _result_payload(output_words) == 3
    assert _result_logical_type(output_words) == int(LogicalType.UINT64)


@cocotb.test()
async def record_batch_repeated_batches_reuse_module(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    await _reset(dut)
    for values, opcode in (([1, 2, 3], OperationCode.SUM), ([9, -9, 4], OperationCode.MAX)):
        columns = [(LogicalType.INT32, values), (LogicalType.INT32, [0] * len(values))]
        await _send_words(dut, _words_from_bytes(_encode_batch(columns, opcode, input_column_id=0)))
        output_words = await _collect_output_words(dut)
        await _accept_success_status(dut)
        assert _result_payload(output_words, signed=True) == int(aggregate_values(opcode, values, LogicalType.INT32))


@cocotb.test()
async def record_batch_out_of_range_column_reports_descriptor_error(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    columns = [(LogicalType.INT32, [1, 2]), (LogicalType.INT32, [3, 4])]
    await _reset(dut)
    await _send_words(dut, _words_from_bytes(_encode_batch(columns, OperationCode.SUM, input_column_id=5)))
    await _expect_error_status(dut, StreamAggregationErrorCode.DESCRIPTOR)


@cocotb.test()
async def record_batch_non_int32_selected_column_reports_descriptor_error(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    columns = [(LogicalType.INT32, [1, 2]), (LogicalType.INT64, [3, 4])]
    await _reset(dut)
    await _send_words(dut, _words_from_bytes(_encode_batch(columns, OperationCode.SUM, input_column_id=1)))
    await _expect_error_status(dut, StreamAggregationErrorCode.DESCRIPTOR)


@cocotb.test()
async def record_batch_truncated_payload_reports_descriptor_error(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    columns = [(LogicalType.INT32, [1, 2, 3, 4, 5, 6, 7, 8])]
    words = _words_from_bytes(_encode_batch(columns, OperationCode.SUM, input_column_id=0))
    # 24 descriptor words + 4 payload words + 4 alignment-pad words; cut 6 so
    # input_last lands mid-payload (4 of 8 rows seen).
    await _reset(dut)
    await _send_words(dut, words[:-6])
    await _expect_error_status(dut, StreamAggregationErrorCode.DESCRIPTOR)


def _encode_batch(columns: list[tuple[LogicalType, list[int]]], opcode: OperationCode, *, input_column_id: int) -> bytes:
    row_count = len(columns[0][1])
    descriptor_table_offset = BatchHeader.SIZE_BYTES
    first_payload_offset = _align_up(descriptor_table_offset + len(columns) * ColumnDescriptor.SIZE_BYTES + OperationDescriptor.SIZE_BYTES)

    descriptors = []
    payloads = []
    offset = first_payload_offset
    for logical_type, values in columns:
        payload = struct.pack(f"<{len(values)}{_CODES[logical_type]}", *values)
        descriptors.append(
            ColumnDescriptor(
                field_id=len(descriptors),
                logical_type=logical_type,
                physical_width_bits=_WIDTHS[logical_type] * 8,
                row_count=row_count,
                null_count=0,
                payload_offset=offset,
                payload_length=len(payload),
                group_role=GroupRole.VALUE,
            )
        )
        payloads.append(payload)
        offset = _align_up(offset + len(payload))

    operation = OperationDescriptor(
        operation_id=0,
        opcode=opcode,
        input_column_id=input_column_id,
        output_field_id=0,
        output_type=LogicalType.UINT64 if opcode == OperationCode.COUNT else LogicalType.INT64,
        accumulator_width_bits=64,
    )
    header = BatchHeader(
        schema_hash=0x1234,
        batch_id=9,
        row_count=row_count,
        column_count=len(columns),
        operation_count=1,
        group_key_count=0,
        descriptor_table_offset=descriptor_table_offset,
        payload_offset=first_payload_offset,
        payload_length=offset - first_payload_offset,
        flags=StreamFlag.NONE,
    )

    encoded = header.pack() + b"".join(descriptor.pack() for descriptor in descriptors) + operation.pack()
    for descriptor, payload in zip(descriptors, payloads):
        encoded += b"\x00" * (descriptor.payload_offset - len(encoded))
        encoded += payload
    encoded += b"\x00" * (_align_up(len(encoded)) - len(encoded))
    return encoded


def _align_up(value: int) -> int:
    return (value + _ALIGN - 1) // _ALIGN * _ALIGN


def _words_from_bytes(data: bytes) -> list[int]:
    padded = data + b"\x00" * ((8 - (len(data) % 8)) % 8)
    return [int.from_bytes(padded[offset : offset + 8], "little") for offset in range(0, len(padded), 8)]


async def _reset(dut) -> None:
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _send_words(dut, words: list[int]) -> None:
    # The one-sample-per-cycle payload path deasserts input_ready every other
    # payload cycle, so sample ready in the ReadOnly phase and only advance on
    # an actual fire (pre-fix, post-edge sampling silently dropped words).
    for index, word in enumerate(words):
        dut.input_data.value = word
        dut.input_last.value = int(index == len(words) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


async def _collect_output_words(dut) -> list[int]:
    words: list[int] = []
    dut.output_ready.value = 0
    for _ in range(64):
        if int(dut.output_valid.value) == 1:
            break
        await RisingEdge(dut.clk)
    else:
        raise AssertionError("timed out waiting for output stream")

    held_word = int(dut.output_data.value)
    for _ in range(3):
        await RisingEdge(dut.clk)
        assert int(dut.output_valid.value) == 1
        assert int(dut.output_data.value) == held_word

    dut.output_ready.value = 1
    for _ in range(64):
        await RisingEdge(dut.clk)
        if int(dut.output_valid.value) == 1:
            words.append(int(dut.output_data.value))
            if int(dut.output_last.value) == 1:
                dut.output_ready.value = 0
                return words
    raise AssertionError("timed out collecting output stream")


async def _accept_success_status(dut) -> None:
    dut.status_ready.value = 1
    for _ in range(16):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            assert int(dut.status_error_code.value) == 0
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


async def _expect_error_status(dut, code: StreamAggregationErrorCode) -> None:
    dut.status_ready.value = 1
    for _ in range(32):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == int(code)
            assert int(dut.output_valid.value) == 0
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for error status")


def _result_logical_type(words: list[int]) -> int:
    return (words[8] >> 16) & 0xFFFF


def _result_payload(words: list[int], *, signed: bool = False) -> int:
    value = words[16]
    if signed and value >= 1 << 63:
        value -= 1 << 64
    return value

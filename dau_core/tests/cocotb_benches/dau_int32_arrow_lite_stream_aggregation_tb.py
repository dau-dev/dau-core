from __future__ import annotations

import struct

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from dau_core.aggregations import aggregate_values
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


@cocotb.test()
async def arrow_lite_sum_round_trips_through_result_stream(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    values = [10, -3, 5, 8]
    await _reset(dut)
    await _send_words(dut, _words_from_bytes(_encode_int32_column(values, OperationCode.SUM)))
    output_words = await _collect_output_words(dut)
    await _accept_success_status(dut)

    assert _result_payload(output_words) == int(aggregate_values(OperationCode.SUM, values, LogicalType.INT32))
    assert _result_logical_type(output_words) == int(LogicalType.INT64)


@cocotb.test()
async def arrow_lite_count_outputs_uint64_result(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    values = [1, 2, 3]
    await _reset(dut)
    await _send_words(dut, _words_from_bytes(_encode_int32_column(values, OperationCode.COUNT)))
    output_words = await _collect_output_words(dut)
    await _accept_success_status(dut)

    assert _result_payload(output_words) == int(aggregate_values(OperationCode.COUNT, values, LogicalType.INT32))
    assert _result_logical_type(output_words) == int(LogicalType.UINT64)


@cocotb.test()
async def arrow_lite_malformed_descriptor_reports_error(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    words = _words_from_bytes(_encode_int32_column([1, 2, 3, 4], OperationCode.SUM))
    # Header payload_length says 8 bytes, but column descriptor says 16.
    words[7] = 8
    await _reset(dut)
    await _send_words(dut, words)

    dut.status_ready.value = 1
    for _ in range(32):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == int(StreamAggregationErrorCode.DESCRIPTOR)
            assert int(dut.output_valid.value) == 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for error status")


def _encode_int32_column(values: list[int], opcode: OperationCode) -> bytes:
    payload = struct.pack(f"<{len(values)}i", *values)
    operation = OperationDescriptor(
        operation_id=0,
        opcode=opcode,
        input_column_id=0,
        output_field_id=0,
        output_type=LogicalType.UINT64 if opcode == OperationCode.COUNT else LogicalType.INT64,
        accumulator_width_bits=64,
    )
    header = BatchHeader(
        schema_hash=0x1234,
        batch_id=7,
        row_count=len(values),
        column_count=1,
        operation_count=1,
        group_key_count=0,
        descriptor_table_offset=BatchHeader.SIZE_BYTES,
        payload_offset=BatchHeader.SIZE_BYTES + ColumnDescriptor.SIZE_BYTES + OperationDescriptor.SIZE_BYTES,
        payload_length=len(payload),
        flags=StreamFlag.NONE,
    )
    column = ColumnDescriptor(
        field_id=0,
        logical_type=LogicalType.INT32,
        physical_width_bits=32,
        row_count=len(values),
        null_count=0,
        payload_offset=header.payload_offset,
        payload_length=len(payload),
        group_role=GroupRole.VALUE,
    )
    return header.pack() + column.pack() + operation.pack() + payload


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
    for index, word in enumerate(words):
        while int(dut.input_ready.value) == 0:
            await RisingEdge(dut.clk)
        dut.input_data.value = word
        dut.input_last.value = int(index == len(words) - 1)
        dut.input_valid.value = 1
        await RisingEdge(dut.clk)
        dut.input_valid.value = 0
        dut.input_last.value = 0


async def _collect_output_words(dut) -> list[int]:
    words: list[int] = []
    dut.output_ready.value = 0
    for _ in range(32):
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


def _result_logical_type(words: list[int]) -> int:
    return (words[8] >> 16) & 0xFFFF


def _result_payload(words: list[int]) -> int:
    return words[16]

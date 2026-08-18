from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from dau_core.operators import aggregate_values
from dau_core.stream import LogicalType, OperationCode, StreamAggregationErrorCode


@cocotb.test()
async def stream_wrapper_emits_sum_metadata(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    await _reset(dut)
    values = [10, -4, 7]
    await _send_descriptor(dut, OperationCode.SUM, row_count=len(values), payload_length=len(values) * 4)
    for value in values:
        await _send_value(dut, value)
    await _wait_for_result(dut)

    assert int(dut.error.value) == 0
    assert int(dut.result_value.value.to_signed()) == int(aggregate_values(OperationCode.SUM, values, LogicalType.INT32))
    assert int(dut.result_logical_type.value) == int(LogicalType.INT64)
    assert int(dut.result_payload_length.value) == 8
    assert int(dut.result_row_count.value) == 1


@cocotb.test()
async def stream_wrapper_rejects_malformed_descriptor(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    await _reset(dut)
    await _send_descriptor(dut, OperationCode.SUM, row_count=2, logical_type=LogicalType.FLOAT32, payload_length=8)
    await _wait_for_result(dut)

    assert int(dut.error.value) == 1
    assert int(dut.error_code.value) == int(StreamAggregationErrorCode.DESCRIPTOR)
    assert int(dut.input_ready.value) == 0


async def _reset(dut) -> None:
    dut.rst.value = 1
    dut.descriptor_valid.value = 0
    dut.descriptor_row_count.value = 0
    dut.descriptor_logical_type.value = int(LogicalType.INT32)
    dut.descriptor_physical_width_bits.value = 32
    dut.descriptor_payload_length.value = 0
    dut.descriptor_opcode.value = int(OperationCode.SUM)
    dut.input_valid.value = 0
    dut.input_value.value = 0
    dut.result_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _send_descriptor(
    dut,
    opcode: OperationCode,
    *,
    row_count: int,
    payload_length: int,
    logical_type: LogicalType = LogicalType.INT32,
) -> None:
    while int(dut.descriptor_ready.value) == 0:
        await RisingEdge(dut.clk)
    dut.descriptor_opcode.value = int(opcode)
    dut.descriptor_row_count.value = row_count
    dut.descriptor_logical_type.value = int(logical_type)
    dut.descriptor_physical_width_bits.value = 32
    dut.descriptor_payload_length.value = payload_length
    dut.descriptor_valid.value = 1
    await RisingEdge(dut.clk)
    dut.descriptor_valid.value = 0


async def _send_value(dut, value: int) -> None:
    while int(dut.input_ready.value) == 0:
        await RisingEdge(dut.clk)
    dut.input_value.value = value
    dut.input_valid.value = 1
    await RisingEdge(dut.clk)
    dut.input_valid.value = 0


async def _wait_for_result(dut) -> None:
    for _ in range(16):
        if int(dut.result_valid.value) == 1:
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for result_valid")

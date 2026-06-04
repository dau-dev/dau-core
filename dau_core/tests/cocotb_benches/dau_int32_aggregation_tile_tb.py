from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge

from dau_core.aggregations import aggregate_values
from dau_core.stream import LogicalType, OperationCode


@cocotb.test()
async def tile_sum_happy_path(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    await _reset(dut)
    values = [10, -3, 5]
    for index, value in enumerate(values):
        await _send(dut, OperationCode.SUM, value, last=index == len(values) - 1)

    await RisingEdge(dut.clk)
    assert int(dut.result_valid.value) == 1
    assert int(dut.error.value) == 0
    assert int(dut.result_value.value.to_signed()) == int(aggregate_values(OperationCode.SUM, values, LogicalType.INT32))
    assert int(dut.input_ready.value) == 0


@cocotb.test()
async def tile_rejects_unsupported_opcode(dut):
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))

    await _reset(dut)
    await _send(dut, 7, 1, last=True)

    await RisingEdge(dut.clk)
    assert int(dut.result_valid.value) == 1
    assert int(dut.error.value) == 1
    assert int(dut.result_value.value.to_signed()) == 0


async def _reset(dut) -> None:
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_last.value = 0
    dut.input_value.value = 0
    dut.input_opcode.value = int(OperationCode.SUM)
    dut.result_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _send(dut, opcode: OperationCode | int, value: int, *, last: bool) -> None:
    while int(dut.input_ready.value) == 0:
        await RisingEdge(dut.clk)
    dut.input_valid.value = 1
    dut.input_last.value = int(last)
    dut.input_value.value = value
    dut.input_opcode.value = int(opcode)
    await RisingEdge(dut.clk)
    dut.input_valid.value = 0
    dut.input_last.value = 0

from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import MapConfig, MapErrorCode, MapOp, map_values

_VALUES = [7, -2, 5, 11, 5, 0, -100, 2**31 - 1, -(2**31)]


@cocotb.test()
async def map_add_matches_golden_with_backpressure(dut):
    await _start(dut)
    config = MapConfig(op=MapOp.ADD, constant=1000)
    mapped = await _run_map(dut, _VALUES, config, ready_pattern=(1, 0, 0, 1, 1, 0))
    assert mapped == map_values(_VALUES, config)


@cocotb.test()
async def map_sub_and_wraparound(dut):
    await _start(dut)
    config = MapConfig(op=MapOp.SUB, constant=1)
    values = [-(2**31), 0, 5]
    mapped = await _run_map(dut, values, config)
    assert mapped == map_values(values, config)
    assert mapped[0] == 2**31 - 1


@cocotb.test()
async def map_mul_matches_golden(dut):
    await _start(dut)
    config = MapConfig(op=MapOp.MUL, constant=-3)
    mapped = await _run_map(dut, _VALUES, config)
    assert mapped == map_values(_VALUES, config)


@cocotb.test()
async def map_mulshr_derives_minute_buckets(dut):
    await _start(dut)
    # floor(v * 139811 / 2**23) == v // 60 over the trading day
    config = MapConfig(op=MapOp.MULSHR, constant=139811, shift=23)
    seconds = [0, 59, 60, 34200, 34260, 57599, 57600, 86399]
    mapped = await _run_map(dut, seconds, config)
    assert mapped == [s // 60 for s in seconds]


@cocotb.test()
async def map_mulshr_arithmetic_shift_negative(dut):
    await _start(dut)
    config = MapConfig(op=MapOp.MULSHR, constant=1, shift=1)
    values = [-7, -1, 7]
    mapped = await _run_map(dut, values, config)
    assert mapped == map_values(values, config)


@cocotb.test()
async def map_shl_and_bitwise_ops(dut):
    await _start(dut)
    for config in (
        MapConfig(op=MapOp.SHL, shift=16),
        MapConfig(op=MapOp.AND, constant=0x00FF_00FF),
        MapConfig(op=MapOp.OR, constant=0x2A),
    ):
        mapped = await _run_map(dut, _VALUES, config)
        assert mapped == map_values(_VALUES, config)


@cocotb.test()
async def map_odd_row_count(dut):
    await _start(dut)
    config = MapConfig(op=MapOp.ADD, constant=7)
    values = [1, 2, 3]
    mapped = await _run_map(dut, values, config)
    assert mapped == map_values(values, config)


@cocotb.test()
async def map_repeated_batches_relatch_config(dut):
    await _start(dut)
    first = MapConfig(op=MapOp.ADD, constant=5)
    assert await _run_map(dut, _VALUES, first) == map_values(_VALUES, first)
    second = MapConfig(op=MapOp.MUL, constant=2)
    assert await _run_map(dut, _VALUES, second) == map_values(_VALUES, second)


@cocotb.test()
async def map_reserved_opcode_reports_config_error(dut):
    await _start(dut)
    _apply_config(dut, MapConfig(op=MapOp.ADD), row_count=4)
    dut.cfg_op.value = 0  # reserved opcode
    dut.output_ready.value = 1  # drain any pre-error output (no pending buffer in this tile)
    await _send_values(dut, [1, 2, 3, 4])
    await _expect_error(dut, MapErrorCode.CONFIG)


@cocotb.test()
async def map_truncated_stream_reports_stream_error(dut):
    await _start(dut)
    _apply_config(dut, MapConfig(op=MapOp.ADD, constant=1), row_count=8)
    dut.output_ready.value = 1  # drain the pre-error output words
    await _send_values(dut, [1, 2, 3, 4])  # input_last after 4 of 8 rows
    await _expect_error(dut, MapErrorCode.STREAM)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    for name in ("cfg_op", "cfg_constant", "cfg_shift", "cfg_row_count"):
        getattr(dut, name).value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


def _apply_config(dut, config: MapConfig, *, row_count: int) -> None:
    dut.cfg_op.value = int(config.op)
    dut.cfg_constant.value = config.constant & 0xFFFFFFFF
    dut.cfg_shift.value = config.shift
    dut.cfg_row_count.value = row_count


async def _run_map(dut, values, config, *, ready_pattern=(1,)):
    _apply_config(dut, config, row_count=len(values))
    send = cocotb.start_soon(_send_values(dut, values))
    samples = await _collect_output(dut, ready_pattern)
    await send
    await _accept_success_status(dut)
    # 1:1 tile: word-padded output, row_count rows meaningful
    assert len(samples) in (len(values), len(values) + 1)
    return samples[: len(values)]


async def _send_values(dut, values) -> None:
    words = []
    for offset in range(0, len(values), 2):
        low = values[offset] & 0xFFFFFFFF
        high = (values[offset + 1] & 0xFFFFFFFF) if offset + 1 < len(values) else 0
        words.append((high << 32) | low)
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


async def _collect_output(dut, ready_pattern) -> list[int]:
    samples: list[int] = []
    pattern_index = 0
    for _ in range(1024):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        pattern_index += 1
        dut.output_ready.value = ready
        await ReadOnly()
        fired = ready == 1 and int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        await RisingEdge(dut.clk)
        if fired:
            samples.append(_signed(word & 0xFFFFFFFF))
            samples.append(_signed(word >> 32))
            if last == 1:
                dut.output_ready.value = 0
                return samples
    raise AssertionError("timed out collecting map output")


async def _accept_success_status(dut) -> None:
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


async def _expect_error(dut, code: MapErrorCode) -> None:
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == int(code)
            assert int(dut.output_valid.value) == 0
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for error status")


def _signed(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value

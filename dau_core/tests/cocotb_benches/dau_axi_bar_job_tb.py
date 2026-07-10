from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import BarAggregate, bar_aggregate_rows

INPUT_ADDRESS = 0x0000_0000
OUTPUT_ADDRESS = 0x0004_0000

ERR_JOB_LENGTH = 0xFE

_TRADES = [
    (570, 100, 10, 500_000),
    (570, 105, 3, 0),
    (570, 95, 2, 250_000),
    (570, 102, 1, 999_999),
    (571, 102, 7, 0),
    (571, 99, 4, 100_000),
    (572, -3, 0, 1),
]


@cocotb.test()
async def axi_bar_job_matches_golden(dut):
    await _start(dut)
    bars = await _run_job(dut, _TRADES)
    assert bars == bar_aggregate_rows(_TRADES)


@cocotb.test()
async def axi_bar_job_multi_burst(dut):
    await _start(dut)
    # 200 rows = 400 words = 25 full bursts, with key changes inside and
    # across burst boundaries
    rows = [(index // 7, 100 + (index % 13) - 6, 1 + index % 5, index % 3) for index in range(200)]
    bars = await _run_job(dut, rows)
    assert bars == bar_aggregate_rows(rows)


@cocotb.test()
async def axi_bar_job_partial_final_burst(dut):
    await _start(dut)
    # 9 rows = 18 words: one full 16-beat burst plus a 2-beat tail
    rows = [(5, 10 + index, 1, 0) for index in range(9)]
    bars = await _run_job(dut, rows)
    assert bars == bar_aggregate_rows(rows)


@cocotb.test()
async def axi_bar_job_single_row(dut):
    await _start(dut)
    bars = await _run_job(dut, [(9, -42, 5, 0)])
    assert bars == [BarAggregate(key=9, open=-42, close=-42, low=-42, high=-42, shares=5, frac=0, count=1)]


@cocotb.test()
async def axi_bar_job_repeated_jobs(dut):
    await _start(dut)
    assert await _run_job(dut, _TRADES) == bar_aggregate_rows(_TRADES)
    second = [(572, 100, 1, 0)]
    assert await _run_job(dut, second) == bar_aggregate_rows(second)
    assert await _run_job(dut, _TRADES) == bar_aggregate_rows(_TRADES)


@cocotb.test()
async def axi_bar_job_misaligned_length_reports_error(dut):
    await _start(dut)
    await _kick_job(dut, INPUT_ADDRESS, 24, OUTPUT_ADDRESS)  # not a multiple of 16
    await _wait_done(dut)
    assert int(dut.error.value) == 1
    assert int(dut.error_code.value) == ERR_JOB_LENGTH
    assert int(dut.result_length_bytes.value) == 0


@cocotb.test()
async def axi_bar_job_zero_length_reports_error(dut):
    await _start(dut)
    await _kick_job(dut, INPUT_ADDRESS, 0, OUTPUT_ADDRESS)
    await _wait_done(dut)
    assert int(dut.error.value) == 1
    assert int(dut.error_code.value) == ERR_JOB_LENGTH


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.start.value = 0
    dut.input_address.value = 0
    dut.input_length_bytes.value = 0
    dut.output_address.value = 0
    dut.bd_write.value = 0
    dut.bd_index.value = 0
    dut.bd_wdata.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run_job(dut, rows):
    words = []
    for key, price, shares, frac in rows:
        words.append(((price & 0xFFFFFFFF) << 32) | (key & 0xFFFFFFFF))
        words.append(((frac & 0xFFFFFFFF) << 32) | (shares & 0xFFFFFFFF))
    await _backdoor_load(dut, INPUT_ADDRESS // 8, words)
    await _kick_job(dut, INPUT_ADDRESS, len(words) * 8, OUTPUT_ADDRESS)
    await _wait_done(dut)
    assert int(dut.error.value) == 0, f"job errored with code {int(dut.error_code.value)}"
    result_bytes = int(dut.result_length_bytes.value)
    assert result_bytes % 48 == 0, "bar records are 48 bytes"
    record_words = await _backdoor_read(dut, OUTPUT_ADDRESS // 8, result_bytes // 8)
    bars = [_decode_bar(record_words[offset : offset + 6]) for offset in range(0, len(record_words), 6)]
    assert int(dut.bar_count.value) == len(bars)
    return bars


async def _kick_job(dut, input_address: int, input_length_bytes: int, output_address: int) -> None:
    dut.input_address.value = input_address
    dut.input_length_bytes.value = input_length_bytes
    dut.output_address.value = output_address
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0


async def _wait_done(dut) -> None:
    for _ in range(20_000):
        await ReadOnly()
        if int(dut.done.value) == 1:
            await RisingEdge(dut.clk)
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for job done")


async def _backdoor_load(dut, base_index: int, words: list[int]) -> None:
    for offset, word in enumerate(words):
        dut.bd_index.value = base_index + offset
        dut.bd_wdata.value = word
        dut.bd_write.value = 1
        await RisingEdge(dut.clk)
    dut.bd_write.value = 0
    await RisingEdge(dut.clk)


async def _backdoor_read(dut, base_index: int, count: int) -> list[int]:
    words = []
    for offset in range(count):
        dut.bd_index.value = base_index + offset
        await ReadOnly()
        words.append(int(dut.bd_rdata.value))
        await RisingEdge(dut.clk)
    return words


def _decode_bar(words: list[int]) -> BarAggregate:
    return BarAggregate(
        key=_signed32(words[0] & 0xFFFFFFFF),
        open=_signed32(words[0] >> 32),
        close=_signed32(words[1] & 0xFFFFFFFF),
        low=_signed32(words[1] >> 32),
        high=_signed32(words[2] & 0xFFFFFFFF),
        shares=_signed64(words[3]),
        frac=_signed64(words[4]),
        count=words[5],
    )


def _signed32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


def _signed64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value

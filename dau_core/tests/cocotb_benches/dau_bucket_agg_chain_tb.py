from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import (
    FieldStats,
    GroupedFieldAggregate,
    bars_from_field_aggregate,
    grouped_field_aggregate_rows,
    time_bucket_key_rows,
)
from dau_core.temporal import signed_volume, time_bars

BUCKET_SHIFT = 3  # bar width 8
BUCKET_BITS = 16

# aligned market rows (symbol, time, price, size, bid, ask), pre-sorted by (symbol, time)
_MARKET = [
    (5, 100, 12, 3, 10, 12),
    (5, 103, 15, 2, 11, 13),
    (5, 112, 8, 4, 9, 11),  # bucket 14 (new bar)
    (7, 100, 50, 7, 48, 52),
    (7, 130, 55, 4, 51, 54),
    (9, 64, 100, 1, 99, 101),
]


def _bucket_rows(market):
    # (partition=symbol, payload_hi=price, time, payload2=size, payload3=signed_volume)
    return [(sym, price, time, size, signed_volume(price, size, bid, ask)) for sym, time, price, size, bid, ask in market]


@cocotb.test()
async def bar_stage_matches_generic_aggregate(dut):
    await _start(dut)
    rows = _bucket_rows(_MARKET)
    groups = await _run(dut, rows)
    assert groups == grouped_field_aggregate_rows(time_bucket_key_rows(rows, bucket_shift=BUCKET_SHIFT, bucket_bits=BUCKET_BITS))


@cocotb.test()
async def bar_stage_selects_to_time_bars(dut):
    await _start(dut)
    groups = await _run(dut, _bucket_rows(_MARKET))
    bars = bars_from_field_aggregate(groups)
    golden = {(bar.symbol_id, bar.open_time >> BUCKET_SHIFT): bar for bar in time_bars(_MARKET, bar_width=1 << BUCKET_SHIFT)}
    assert len(bars) == len(golden)
    for bar in bars:
        ref = golden[(bar.key >> BUCKET_BITS, bar.key & ((1 << BUCKET_BITS) - 1))]
        assert (bar.open, bar.high, bar.low, bar.close) == (ref.open, ref.high, ref.low, ref.close)
        assert (bar.shares, bar.frac, bar.count) == (ref.volume, ref.signed_volume, ref.count)


@cocotb.test()
async def bar_stage_backpressure(dut):
    await _start(dut)
    rows = _bucket_rows(_MARKET)
    groups = await _run(dut, rows, ready_pattern=(1, 0, 0, 1, 0))
    assert groups == grouped_field_aggregate_rows(time_bucket_key_rows(rows, bucket_shift=BUCKET_SHIFT, bucket_bits=BUCKET_BITS))


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    dut.cfg_bucket_shift.value = BUCKET_SHIFT
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, *, ready_pattern=(1,)):
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_output(dut, ready_pattern)
    await send
    count = await _accept_success_status(dut)
    assert len(words) % 11 == 0, "grouped-field records are 11 words"
    assert count == len(words) // 11
    return [_decode(words[offset : offset + 11]) for offset in range(0, len(words), 11)]


def _field(lastfirst, minmax, total) -> FieldStats:
    return FieldStats(
        first=_s32(lastfirst & 0xFFFFFFFF),
        last=_s32(lastfirst >> 32),
        minimum=_s32(minmax & 0xFFFFFFFF),
        maximum=_s32(minmax >> 32),
        total=_s64(total),
    )


def _decode(w) -> GroupedFieldAggregate:
    return GroupedFieldAggregate(
        key=_s32(w[0] & 0xFFFFFFFF),
        fields=(_field(w[1], w[2], w[3]), _field(w[4], w[5], w[6]), _field(w[7], w[8], w[9])),
        count=w[10],
    )


async def _send_rows(dut, rows) -> None:
    words = []
    for partition, payload_hi, time, payload2, payload3 in rows:
        words.append(((payload_hi & 0xFFFFFFFF) << 32) | (partition & 0xFFFFFFFF))
        words.append(time & ((1 << 64) - 1))
        words.append(((payload3 & 0xFFFFFFFF) << 32) | (payload2 & 0xFFFFFFFF))
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


async def _collect_output(dut, ready_pattern):
    words = []
    for pattern_index in range(16384):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        dut.output_ready.value = ready
        await ReadOnly()
        fired = ready == 1 and int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                dut.output_ready.value = 0
                return words
    raise AssertionError("timed out collecting bar-stage output")


async def _accept_success_status(dut) -> int:
    dut.status_ready.value = 1
    for _ in range(128):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            count = int(dut.group_count.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return count
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


def _s32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


def _s64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value

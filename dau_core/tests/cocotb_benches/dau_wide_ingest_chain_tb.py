from __future__ import annotations

from collections import Counter

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import gearbox_records, mask_dispatch_records

_BEAT_WORDS = 8
_RECORD_WORDS = 3
_NUM_PARTITIONS = 8
_MASK64 = (1 << 64) - 1


def _records(count: int) -> list[tuple[int, int, int]]:
    return [
        (
            ((index + 1) << 32) | (index & (_NUM_PARTITIONS - 1)),
            index,
            (0xDAD0 << 48) | index,
        )
        for index in range(count)
    ]


def _pack_beats(words: list[int]) -> list[int]:
    padded = list(words)
    while len(padded) % _BEAT_WORDS:
        padded.append(0xDEAD_BEEF_DEAD_BEEF)
    return [
        sum((word & _MASK64) << (64 * lane) for lane, word in enumerate(padded[offset : offset + _BEAT_WORDS]))
        for offset in range(0, len(padded), _BEAT_WORDS)
    ]


async def _start(dut) -> None:
    cocotb.start_soon(Clock(dut.clk, 4, unit="ns").start(start_high=False))
    dut.rst.value = 1
    dut.cfg_input_records.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    dut.gearbox_status_ready.value = 0
    for _ in range(3):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _send(dut, beats: list[int]) -> None:
    for index, beat in enumerate(beats):
        dut.input_data.value = beat
        dut.input_last.value = int(index == len(beats) - 1)
        dut.input_valid.value = 1
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0


@cocotb.test()
async def wide_ingest_chain_routes_at_one_record_per_cycle(dut) -> None:
    await _start(dut)
    records = _records(1027)
    flat_words = [word for record in records for word in record]
    beats = _pack_beats(flat_words)
    reframed = gearbox_records(flat_words, record_words=_RECORD_WORDS, record_count=len(records))
    expected = mask_dispatch_records(reframed, num_partitions=_NUM_PARTITIONS)

    dut.cfg_input_records.value = len(records)
    dut.output_ready.value = (1 << _NUM_PARTITIONS) - 1
    dut.status_ready.value = (1 << _NUM_PARTITIONS) - 1
    dut.gearbox_status_ready.value = 1
    await RisingEdge(dut.clk)
    sender = cocotb.start_soon(_send(dut, beats))

    lane_words: list[list[int]] = [[] for _ in range(_NUM_PARTITIONS)]
    done = [False] * _NUM_PARTITIONS
    accepted_records = 0
    first_record_tick = None
    last_record_tick = None

    for tick in range(20_000):
        await ReadOnly()
        assert int(dut.gearbox_status_valid.value) == 0, "gearbox reported a framing error"
        assert int(dut.status_error.value) == 0, "dispatcher lane reported an error"

        if int(dut.record_valid.value) == 1 and int(dut.record_ready.value) == 1:
            accepted_records += 1
            first_record_tick = tick if first_record_tick is None else first_record_tick
            last_record_tick = tick

        valid = int(dut.output_valid.value)
        data = int(dut.output_data.value)
        last = int(dut.output_last.value)
        for lane in range(_NUM_PARTITIONS):
            if valid >> lane & 1:
                lane_words[lane].append((data >> (64 * lane)) & _MASK64)
                if last >> lane & 1:
                    done[lane] = True

        await RisingEdge(dut.clk)
        if accepted_records == len(records) and all(done):
            break
    else:
        raise AssertionError(
            f"timed out after accepting {accepted_records}/{len(records)} records; lane words={[len(words) for words in lane_words]}"
        )

    await sender
    assert accepted_records == len(records)
    assert first_record_tick is not None and last_record_tick is not None
    record_cycles = last_record_tick - first_record_tick + 1
    measured_rate = accepted_records / record_cycles
    dut._log.info(
        "RATE wide-ingest-chain measured=%.6f records/cycle records=%d cycles=%d",
        measured_rate,
        accepted_records,
        record_cycles,
    )
    assert 0.99 <= measured_rate <= 1.01, (
        f"wide ingest accepted {accepted_records} records in {record_cycles} cycles ({measured_rate:.6f} records/cycle), expected ~1"
    )

    assert all(len(words) % _RECORD_WORDS == 0 for words in lane_words)
    received = [[tuple(words[offset : offset + _RECORD_WORDS]) for offset in range(0, len(words), _RECORD_WORDS)] for words in lane_words]
    assert received == expected, "record routed to wrong lane or lane order changed"

    received_union = [record for lane in received for record in lane]
    assert Counter(received_union) == Counter(reframed), "record drop or duplication"
    assert sorted(received_union, key=lambda record: record[1]) == reframed, "input record sequence changed"
    for lane, lane_records in enumerate(received):
        assert all((record[0] & (_NUM_PARTITIONS - 1)) == lane for record in lane_records)

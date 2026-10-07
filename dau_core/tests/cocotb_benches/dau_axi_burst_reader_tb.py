"""Width-parameterized burst-reader bench: an AXI4 read-slave model serves
a deterministic memory image (each 32-bit lane = its global word index)
with periodic backpressure, and the collected stream must reproduce the
image exactly at every DATA_WIDTH tier — the same contract at
64/128/256/512 is the precompiled-catalog guarantee.

Handshake discipline: sample every valid/ready pair in the ReadOnly phase
(stable pre-edge values), apply state and drive next-cycle values after
the clock edge."""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge


def _beat_value(beat_index: int, data_width: int) -> int:
    lanes = data_width // 32
    value = 0
    for lane in range(lanes):
        value |= ((beat_index * lanes + lane) & 0xFFFF_FFFF) << (32 * lane)
    return value


@cocotb.test()
async def burst_reader_streams_the_window_exactly(dut):
    data_width = len(dut.m_axi_rdata)
    bytes_per_beat = data_width // 8
    cocotb.start_soon(Clock(dut.clk, 4, unit="ns").start())

    total_beats = 53  # spans multiple bursts at BURST_BEATS=16, ragged tail
    base = 0x1000

    dut.rst.value = 1
    dut.start.value = 0
    dut.stream_ready.value = 0
    dut.m_axi_arready.value = 0
    dut.m_axi_rvalid.value = 0
    dut.m_axi_rlast.value = 0
    dut.m_axi_rresp.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)

    dut.read_address.value = base
    dut.read_length_bytes.value = total_beats * bytes_per_beat
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    dut.m_axi_arready.value = 1

    pending: list[tuple[int, int]] = []  # (next beat index, beats left)
    r_active = False
    collected: list[int] = []
    saw_last = False

    # 1-based: the counter used to increment at the top of the body, and the
    # gap patterns below are phased off that
    for cycle in range(1, 20_001):
        await ReadOnly()
        ar_fire = bool(dut.m_axi_arvalid.value) and bool(dut.m_axi_arready.value)
        if ar_fire:
            ar_beat = int(dut.m_axi_araddr.value) // bytes_per_beat
            ar_beats = int(dut.m_axi_arlen.value) + 1
        r_fire = bool(dut.m_axi_rvalid.value) and bool(dut.m_axi_rready.value)
        s_fire = bool(dut.stream_valid.value) and bool(dut.stream_ready.value)
        if s_fire:
            collected.append(int(dut.stream_data.value))
            if dut.stream_last.value:
                saw_last = True
        done = bool(dut.done.value)

        await RisingEdge(dut.clk)

        if ar_fire:
            pending.append((ar_beat, ar_beats))
        if r_fire:
            beat, left = pending[0]
            if left == 1:
                pending.pop(0)
            else:
                pending[0] = (beat + 1, left - 1)
            r_active = False
        # drive next-cycle values: R data with a gap every 5th cycle
        if pending and not r_active and cycle % 5 != 0:
            beat, left = pending[0]
            dut.m_axi_rvalid.value = 1
            dut.m_axi_rdata.value = _beat_value(beat, data_width)
            dut.m_axi_rlast.value = 1 if left == 1 else 0
            r_active = True
        elif not pending or not r_active:
            dut.m_axi_rvalid.value = 0
            dut.m_axi_rlast.value = 0
        dut.stream_ready.value = 1 if cycle % 7 != 0 else 0

        if len(collected) == total_beats and done:
            break

    assert len(collected) == total_beats, f"streamed {len(collected)} of {total_beats} beats"
    assert saw_last, "stream_last never asserted on the final beat"
    expected = [_beat_value((base // bytes_per_beat) + i, data_width) for i in range(total_beats)]
    assert collected == expected, "stream data does not reproduce the memory image"
    assert not dut.error.value, "reader flagged an error on a clean run"

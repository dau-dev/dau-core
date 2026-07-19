from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import MapConfig, MapErrorCode, MapOp, map_rows

_MASK32 = 0xFFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# one keyed (key, value) row per 64-bit word: {value[31:0], key[31:0]}
_ROWS = [
    (34200, 1000),
    (34259, 2000),
    (34260, 1500),
    (34261, -500),
]

# minute-bucket derivation on the key lane: bucket = time * 139811 >>> 23
_BUCKET = MapConfig(op=MapOp.MULSHR, constant=139811, shift=23)


@cocotb.test()
async def every_op_matches_golden(dut):
    await _start(dut)
    rows = [(3, 7), (100, -100), (0, 1), (-7, 5)]
    for op in MapOp:
        config = MapConfig(op=op, constant=5, shift=3)
        out = await _run(dut, rows, config)
        assert out == map_rows(rows, config), op.name


@cocotb.test()
async def key_lane_map_derives_buckets(dut):
    # cfg_target_lane=1: the ALU maps the key lane (time -> minute bucket),
    # the value lane passes through unchanged
    await _start(dut)
    out = await _run(dut, _ROWS, _BUCKET, on_key=True)
    assert out == map_rows(_ROWS, _BUCKET, on_key=True)
    assert out[0] == (570, 1000)  # 34200 * 139811 >>> 23 = 570


@cocotb.test()
async def value_lane_map_key_passthrough(dut):
    await _start(dut)
    config = MapConfig(op=MapOp.ADD, constant=1000)
    out = await _run(dut, _ROWS, config)
    assert out == map_rows(_ROWS, config)
    for (key, _), (mapped_key, _) in zip(_ROWS, out):
        assert mapped_key == key, "value-lane map clobbered the key"


@cocotb.test()
async def int32_wraparound_extremes(dut):
    await _start(dut)
    rows = [(_INT32_MAX, _INT32_MIN), (_INT32_MIN, _INT32_MAX), (-1, -1)]
    for on_key in (False, True):
        for op in (MapOp.ADD, MapOp.SUB, MapOp.MUL, MapOp.MULSHR):
            config = MapConfig(op=op, constant=_INT32_MAX, shift=1)
            out = await _run(dut, rows, config, on_key=on_key)
            assert out == map_rows(rows, config, on_key=on_key), f"{op.name} on_key={on_key}"


@cocotb.test()
async def mulshr_arithmetic_shift_on_negative_product(dut):
    await _start(dut)
    rows = [(0, -7)]
    config = MapConfig(op=MapOp.MULSHR, constant=1, shift=1)
    out = await _run(dut, rows, config)
    assert out == map_rows(rows, config) == [(0, -4)]  # -7 >>> 1 floors to -4


@cocotb.test()
async def last_driven_mode_trusts_upstream_last(dut):
    # cfg_row_count == 0: no stream-length check; the upstream last decides
    await _start(dut)
    out = await _run(dut, _ROWS, _BUCKET, on_key=True, count_checked=False)
    assert out == map_rows(_ROWS, _BUCKET, on_key=True)


@cocotb.test()
async def order_preserved_with_backpressure(dut):
    await _start(dut)
    out = await _run(dut, _ROWS, _BUCKET, on_key=True, ready_pattern=(1, 0, 0, 1, 0))
    assert out == map_rows(_ROWS, _BUCKET, on_key=True)


@cocotb.test()
async def reset_clears_state(dut):
    # stall an emitted beat under output backpressure, assert rst, and confirm
    # the tile is quiescent and accepts a fresh batch
    await _start(dut)
    _drive_config(dut, _BUCKET, on_key=True, row_count=0)
    dut.output_ready.value = 0
    await _send_one(dut, _pack_rows(_ROWS)[0])
    found = False
    for _ in range(8):
        await ReadOnly()
        if int(dut.output_valid.value) == 1:
            found = True
            await RisingEdge(dut.clk)
            break
        await RisingEdge(dut.clk)
    assert found, "no stalled output beat to reset"

    dut.rst.value = 1
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)
    await ReadOnly()
    assert int(dut.output_valid.value) == 0
    assert int(dut.status_valid.value) == 0
    await RisingEdge(dut.clk)

    out = await _run(dut, _ROWS, _BUCKET, on_key=True)
    assert out == map_rows(_ROWS, _BUCKET, on_key=True)


@cocotb.test()
async def short_stream_reports_stream_error(dut):
    # count-checked mode: input_last before the declared count is a STREAM
    # error (the row-per-word analog of a torn row)
    await _start(dut)
    _drive_config(dut, _BUCKET, on_key=True, row_count=len(_ROWS) + 2)
    drain = cocotb.start_soon(_drain_words(dut))
    send = cocotb.start_soon(_send_words(dut, _pack_rows(_ROWS)))
    await _expect_error(dut, MapErrorCode.STREAM)
    await send
    drain.kill()
    dut.output_ready.value = 0
    await _expect_recovers(dut)


@cocotb.test()
async def long_stream_reports_stream_error(dut):
    # count-checked mode: more rows than the declared count
    await _start(dut)
    _drive_config(dut, _BUCKET, on_key=True, row_count=2)
    drain = cocotb.start_soon(_drain_words(dut))
    send = cocotb.start_soon(_send_words(dut, _pack_rows(_ROWS)))
    await _expect_error(dut, MapErrorCode.STREAM)
    await send
    drain.kill()
    dut.output_ready.value = 0
    await _expect_recovers(dut)


@cocotb.test()
async def reserved_op_reports_config_error(dut):
    await _start(dut)
    _drive_config(dut, _BUCKET, on_key=True, row_count=len(_ROWS))
    dut.cfg_op.value = 0  # reserved: an unconfigured tile fails explicitly
    drain = cocotb.start_soon(_drain_words(dut))
    send = cocotb.start_soon(_send_words(dut, _pack_rows(_ROWS)))
    await _expect_error(dut, MapErrorCode.CONFIG)
    await send
    drain.kill()
    dut.output_ready.value = 0
    await _expect_recovers(dut)


@cocotb.test()
async def error_status_survives_backpressure(dut):
    # while status_ready is held low the error close-out stays stable and the
    # tile refuses new input; recovery only after the handshake
    await _start(dut)
    _drive_config(dut, _BUCKET, on_key=True, row_count=len(_ROWS) + 2)
    dut.status_ready.value = 0
    drain = cocotb.start_soon(_drain_words(dut))
    send = cocotb.start_soon(_send_words(dut, _pack_rows(_ROWS)))
    await _wait_status_asserted(dut)
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "error status not held"
        assert int(dut.status_error.value) == 1
        assert int(dut.status_error_code.value) == MapErrorCode.STREAM
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)
    await _expect_error(dut, MapErrorCode.STREAM)
    await send
    drain.kill()
    dut.output_ready.value = 0
    await _expect_recovers(dut)


@cocotb.test()
async def success_status_survives_backpressure(dut):
    # the per-batch success close-out must hold stable while status_ready is
    # low and the tile must refuse new input until the handshake completes
    await _start(dut)
    _drive_config(dut, _BUCKET, on_key=True, row_count=len(_ROWS))
    dut.status_ready.value = 0
    send = cocotb.start_soon(_send_words(dut, _pack_rows(_ROWS)))
    await _drain_until_status(dut)
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "success status not held"
        assert int(dut.status_error.value) == 0
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)
    await _accept_success_status(dut)
    await send
    await _expect_recovers(dut)


@cocotb.test()
async def repeated_batches_relatch_config(dut):
    # back-to-back batches with distinct configs each latch fresh at IDLE
    await _start(dut)
    for config, on_key in (
        (MapConfig(op=MapOp.ADD, constant=1000), False),
        (_BUCKET, True),
        (MapConfig(op=MapOp.SHL, shift=2), False),
    ):
        out = await _run(dut, _ROWS, config, on_key=on_key)
        assert out == map_rows(_ROWS, config, on_key=on_key), (config, on_key)


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0xB0BA)
    ops = list(MapOp)
    for trial in range(40):
        rows = []
        for _ in range(rng.randint(1, 10)):
            row = tuple(rng.choice((_INT32_MIN, _INT32_MAX, 0, -1)) if rng.random() < 0.1 else rng.randint(-1000, 1000) for _ in range(2))
            rows.append(row)
        config = MapConfig(
            op=rng.choice(ops),
            constant=rng.choice((_INT32_MIN, _INT32_MAX, 0)) if rng.random() < 0.15 else rng.randint(-1000, 1000),
            shift=rng.randint(0, 31),
        )
        on_key = bool(rng.getrandbits(1))
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):
            pattern = pattern + (1,)
        out = await _run(dut, rows, config, on_key=on_key, ready_pattern=pattern)
        assert out == map_rows(rows, config, on_key=on_key), f"trial {trial}: rows={rows} config={config} on_key={on_key}"


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _drive_config(dut, config: MapConfig, *, on_key: bool, row_count: int) -> None:
    dut.cfg_target_lane.value = int(on_key)
    dut.cfg_op.value = int(config.op)
    dut.cfg_constant.value = config.constant & _MASK32
    dut.cfg_shift.value = config.shift
    dut.cfg_row_count.value = row_count


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    _drive_config(dut, MapConfig(op=MapOp.ADD), on_key=False, row_count=0)
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, config: MapConfig, *, on_key=False, count_checked=True, ready_pattern=(1,)):
    _drive_config(dut, config, on_key=on_key, row_count=len(rows) if count_checked else 0)
    send = cocotb.start_soon(_send_words(dut, _pack_rows(rows)))
    words = await _collect_output(dut, ready_pattern, expect_rows=len(rows))
    await send
    return [(_signed32(word & _MASK32), _signed32(word >> 32)) for word in words]


def _pack_rows(rows) -> list[int]:
    return [((value & _MASK32) << 32) | (key & _MASK32) for key, value in rows]


async def _send_one(dut, word: int) -> None:
    dut.input_data.value = word
    dut.input_last.value = 0
    dut.input_valid.value = 1
    while True:
        await ReadOnly()
        fired = int(dut.input_ready.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            break
    dut.input_valid.value = 0


async def _send_words(dut, words) -> None:
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


async def _drain_words(dut) -> None:
    dut.output_ready.value = 1
    while True:
        await RisingEdge(dut.clk)


async def _collect_output(dut, ready_pattern, *, expect_rows: int) -> list[int]:
    words: list[int] = []
    pattern_index = 0
    held = None
    collected = False
    for _ in range(200000):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        pattern_index += 1
        dut.output_ready.value = ready
        await ReadOnly()
        valid = int(dut.output_valid.value) == 1
        fired = ready == 1 and valid
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        if held is not None:
            # valid/ready hold rule: a stalled beat stays asserted and
            # byte-stable until it fires
            assert valid, "output_valid dropped while a beat was stalled"
            assert (word, last) == held, "output beat changed while stalled"
        held = None if (fired or not valid) else (word, last)
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                assert len(words) == expect_rows, f"expected {expect_rows} rows, got {len(words)}"
                collected = True
                break
    if not collected:
        raise AssertionError(f"timed out collecting map output ({len(words)}/{expect_rows} rows)")
    await _accept_success_status(dut)
    return words


async def _accept_success_status(dut) -> None:
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    for _ in range(4096):
        await ReadOnly()
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0, "success close-out flagged an error"
            assert int(dut.status_error_code.value) == 0
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            dut.output_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


async def _drain_until_status(dut) -> None:
    # hold output_ready high so the batch drains and its success close-out
    # asserts while status_ready is still held low; return in the post-clock
    # phase (not ReadOnly) so the caller may ReadOnly
    dut.output_ready.value = 1
    for _ in range(4096):
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if found:
            return
    raise AssertionError("success status never asserted")


async def _wait_status_asserted(dut) -> None:
    # return in the post-clock phase (not ReadOnly) so the caller may ReadOnly
    for _ in range(64):
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if found:
            return
    raise AssertionError("status never asserted")


async def _expect_error(dut, code: MapErrorCode) -> None:
    dut.status_ready.value = 1
    for _ in range(4096):
        await ReadOnly()
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == int(code), f"status code {int(dut.status_error_code.value)} != {int(code)}"
            assert int(dut.output_valid.value) == 0, "error close-out left an output beat in flight"
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for error status")


async def _expect_recovers(dut) -> None:
    # after any close-out the tile returns to IDLE and maps the next batch
    out = await _run(dut, _ROWS, _BUCKET, on_key=True)
    assert out == map_rows(_ROWS, _BUCKET, on_key=True)

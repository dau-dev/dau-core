from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import MapErrorCode, MapOp, RowField, RowMapConfig, map_row_fields

_MASK32 = 0xFFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# (key, value, aux0, aux1) — the q6 field map is
# (shipdate-bucket, extendedprice, discount, quantity)
_ROWS = [
    (568, 1000, 5 << 10, 10),
    (569, 2000, 6 << 10, 30),
    (568, 1500, 7 << 10, 23),
    (570, -500, 6 << 10, 24),
]

# fixed-point revenue: value = value * aux0 >>> 15
_Q6_REVENUE = RowMapConfig(op=MapOp.MULSHR, dst=RowField.VALUE, src_a=RowField.VALUE, src_b=RowField.AUX0, shift=15)


@cocotb.test()
async def q6_revenue_derivation(dut):
    await _start(dut)
    out = await _run(dut, _ROWS, _Q6_REVENUE)
    assert out == map_row_fields(_ROWS, _Q6_REVENUE)
    # spot-check the fixed-point arithmetic: 1000 * (5<<10) >> 15 = 156
    assert out[0] == (568, 156, 5 << 10, 10)


@cocotb.test()
async def every_op_matches_golden(dut):
    await _start(dut)
    rows = [(3, 7, -2, 5), (100, -100, 30, 2), (0, 1, 2, 3)]
    for op in MapOp:
        config = RowMapConfig(op=op, dst=RowField.AUX1, src_a=RowField.VALUE, src_b=RowField.AUX0, shift=3)
        assert await _run(dut, rows, config) == map_row_fields(rows, config), op.name


@cocotb.test()
async def dst_field_variants_pass_others_through(dut):
    await _start(dut)
    for dst in RowField:
        config = RowMapConfig(op=MapOp.ADD, dst=dst, src_a=RowField.KEY, src_b=RowField.AUX1)
        out = await _run(dut, _ROWS, config)
        assert out == map_row_fields(_ROWS, config), dst.name
        for row, mapped in zip(_ROWS, out):
            for field in RowField:
                if field != dst:
                    assert mapped[field] == row[field], f"{dst.name} clobbered {field.name}"


@cocotb.test()
async def dst_equals_src_in_place_update(dut):
    await _start(dut)
    config = RowMapConfig(op=MapOp.MUL, dst=RowField.VALUE, src_a=RowField.VALUE, src_b=RowField.VALUE)
    assert await _run(dut, _ROWS, config) == map_row_fields(_ROWS, config)


@cocotb.test()
async def int32_wraparound_extremes(dut):
    await _start(dut)
    rows = [
        (_INT32_MAX, 1, _INT32_MAX, _INT32_MIN),
        (_INT32_MIN, -1, _INT32_MIN, _INT32_MAX),
        (-1, -1, -1, -1),
    ]
    for op in (MapOp.ADD, MapOp.SUB, MapOp.MUL, MapOp.MULSHR):
        config = RowMapConfig(op=op, dst=RowField.KEY, src_a=RowField.KEY, src_b=RowField.VALUE, shift=1)
        assert await _run(dut, rows, config) == map_row_fields(rows, config), op.name


@cocotb.test()
async def mulshr_arithmetic_shift_on_negative_product(dut):
    await _start(dut)
    rows = [(-7, 1, 0, 0)]
    config = RowMapConfig(op=MapOp.MULSHR, dst=RowField.KEY, src_a=RowField.KEY, src_b=RowField.VALUE, shift=1)
    assert await _run(dut, rows, config) == [(-4, 1, 0, 0)]


@cocotb.test()
async def order_and_count_preserved_with_backpressure(dut):
    await _start(dut)
    out = await _run(dut, _ROWS, _Q6_REVENUE, ready_pattern=(1, 0, 0, 1, 0))
    assert out == map_row_fields(_ROWS, _Q6_REVENUE)


@cocotb.test()
async def torn_row_reports_stream_error(dut):
    await _start(dut)
    _drive_config(dut, _Q6_REVENUE)
    # input_last on a w0: a torn row
    await _send_words(dut, [(100 << 32) | 5])
    await _expect_status(dut, error=True, code=MapErrorCode.STREAM)

    # recovers: the next batch maps normally
    assert await _run(dut, _ROWS, _Q6_REVENUE) == map_row_fields(_ROWS, _Q6_REVENUE)


@cocotb.test()
async def torn_row_mid_batch_reports_stream_error(dut):
    await _start(dut)
    _drive_config(dut, _Q6_REVENUE)
    drain = cocotb.start_soon(_drain_words(dut))
    send = cocotb.start_soon(_send_words(dut, _pack_rows(_ROWS[:2]) + [(999 << 32) | 999]))
    await _expect_status(dut, error=True, code=MapErrorCode.STREAM)
    await send
    drain.kill()
    dut.output_ready.value = 0
    assert await _run(dut, _ROWS, _Q6_REVENUE) == map_row_fields(_ROWS, _Q6_REVENUE)


@cocotb.test()
async def reserved_op_drains_batch_then_reports_config_error(dut):
    await _start(dut)
    _drive_config(dut, _Q6_REVENUE)
    dut.cfg_op.value = 0  # reserved: an unconfigured tile fails explicitly
    send = cocotb.start_soon(_send_rows(dut, _ROWS))
    await _expect_status(dut, error=True, code=MapErrorCode.CONFIG)
    await send

    assert await _run(dut, _ROWS, _Q6_REVENUE) == map_row_fields(_ROWS, _Q6_REVENUE)


@cocotb.test()
async def error_status_survives_backpressure(dut):
    # while status_ready is held low the error close-out must stay stable and
    # the tile must refuse new input; recovery only after the handshake
    await _start(dut)
    _drive_config(dut, _Q6_REVENUE)
    dut.status_ready.value = 0
    await _send_words(dut, [(100 << 32) | 5])

    found = False
    for _ in range(8):
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if found:
            break
    assert found, "error status never asserted"
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "error status not held"
        assert int(dut.status_error.value) == 1
        assert int(dut.status_error_code.value) == MapErrorCode.STREAM
        assert int(dut.input_ready.value) == 0, "tile accepted input while status pending"
        await RisingEdge(dut.clk)

    await _expect_status(dut, error=True, code=MapErrorCode.STREAM)
    assert await _run(dut, _ROWS, _Q6_REVENUE) == map_row_fields(_ROWS, _Q6_REVENUE)


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0xA111)
    fields = list(RowField)
    ops = list(MapOp)
    for trial in range(40):
        rows = []
        for _ in range(rng.randint(1, 10)):
            row = []
            for _ in range(4):
                if rng.random() < 0.1:
                    row.append(rng.choice((_INT32_MIN, _INT32_MAX, 0, -1)))
                else:
                    row.append(rng.randint(-1000, 1000))
            rows.append(tuple(row))
        config = RowMapConfig(
            op=rng.choice(ops),
            dst=rng.choice(fields),
            src_a=rng.choice(fields),
            src_b=rng.choice(fields),
            shift=rng.randint(0, 31),
        )
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 6)))
        if not any(pattern):  # a ready pattern must let data through eventually
            pattern = pattern + (1,)
        out = await _run(dut, rows, config, ready_pattern=pattern)
        assert out == map_row_fields(rows, config), f"trial {trial}: rows={rows} config={config}"


# ---- helpers ----


def _signed32(value: int) -> int:
    value &= _MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def _drive_config(dut, config: RowMapConfig) -> None:
    dut.cfg_op.value = int(config.op)
    dut.cfg_dst_field.value = int(config.dst)
    dut.cfg_src_a_field.value = int(config.src_a)
    dut.cfg_src_b_field.value = int(config.src_b)
    dut.cfg_shift.value = config.shift


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    _drive_config(dut, RowMapConfig(op=MapOp.ADD, dst=RowField.KEY, src_a=RowField.KEY, src_b=RowField.KEY))
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, config: RowMapConfig, *, ready_pattern=(1,)):
    _drive_config(dut, config)
    send = cocotb.start_soon(_send_rows(dut, rows))
    words = await _collect_output(dut, ready_pattern, expect_words=2 * len(rows))
    await send
    out = []
    for offset in range(0, len(words), 2):
        w0, w1 = words[offset], words[offset + 1]
        out.append((_signed32(w0 & _MASK32), _signed32(w0 >> 32), _signed32(w1 & _MASK32), _signed32(w1 >> 32)))
    return out


def _pack_rows(rows) -> list[int]:
    words = []
    for key, value, aux0, aux1 in rows:
        words.append(((value & _MASK32) << 32) | (key & _MASK32))
        words.append(((aux1 & _MASK32) << 32) | (aux0 & _MASK32))
    return words


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


async def _send_rows(dut, rows) -> None:
    await _send_words(dut, _pack_rows(rows))


async def _drain_words(dut) -> None:
    dut.output_ready.value = 1
    while True:
        await RisingEdge(dut.clk)


async def _collect_output(dut, ready_pattern, *, expect_words: int) -> list[int]:
    words: list[int] = []
    pattern_index = 0
    held = None
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
            # valid/ready hold rule: a stalled beat must stay asserted and
            # byte-stable until it fires
            assert valid, "output_valid dropped while a beat was stalled"
            assert (word, last) == held, "output beat changed while stalled"
        held = None if (fired or not valid) else (word, last)
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                dut.output_ready.value = 0
                assert len(words) == expect_words, f"expected {expect_words} words, got {len(words)}"
                # a successful batch is silent: the downstream tile closes out
                await _assert_quiescent(dut)
                return words
    raise AssertionError(f"timed out collecting map output ({len(words)}/{expect_words} words)")


async def _assert_quiescent(dut) -> None:
    """After a batch completes, nothing more may appear on the output or
    status streams even with ready held high (silent-success contract)."""
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    for _ in range(6):
        await ReadOnly()
        assert int(dut.output_valid.value) == 0, "output beat after batch completion"
        assert int(dut.status_valid.value) == 0, "status beat after batch completion"
        await RisingEdge(dut.clk)
    dut.output_ready.value = 0
    dut.status_ready.value = 0


async def _expect_status(dut, *, error: bool, code: int = 0) -> None:
    dut.status_ready.value = 1
    for _ in range(4096):
        await ReadOnly()
        fired = int(dut.status_valid.value) == 1
        observed_error = int(dut.status_error.value)
        observed_code = int(dut.status_error_code.value)
        await RisingEdge(dut.clk)
        if fired:
            assert observed_error == int(error)
            if error:
                assert observed_code == code, f"status code {observed_code} != {code}"
            else:
                assert observed_code == 0, f"success close-out carried code {observed_code}"
            dut.status_ready.value = 0
            return
    raise AssertionError("timed out waiting for map status")

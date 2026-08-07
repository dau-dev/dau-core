"""The row predicate filter at field widths other than 32.

The tile's row is a fixed 128-bit transport unit; FIELD_WIDTH says how many
fields that row carries. So int16 packs eight values per row where int32
packs four, which is the reason to support narrow types at all -- more
values per beat, not a smaller beat.

This bench is separate from the 32-bit one on purpose. That one pins the
q6/q19 field map and its directed vectors; this one is generic over
FIELD_WIDTH and proves the parameter actually works rather than merely
elaborating. It compares against a golden computed here from Python ints,
with sign interpretation at the field width under test -- the case a
zero-extending implementation gets wrong.
"""

from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

ROW_BITS = 128

_OP_LT = 3
_OP_GE = 5
_COMBINE_SINGLE = 0


def _field_width() -> int:
    return int(os.environ.get("DAU_ROW_PREDICATE_FIELD_WIDTH", "32"))


def _signed(value: int, width: int) -> int:
    """Interpret a width-bit two's complement field as a Python int."""
    value &= (1 << width) - 1
    return value - (1 << width) if value >= (1 << (width - 1)) else value


def _pack(fields: list[int], width: int) -> tuple[int, int]:
    """Fields little-endian into the 128-bit row, low word first."""
    row = 0
    for index, field in enumerate(fields):
        row |= (field & ((1 << width) - 1)) << (index * width)
    return row & 0xFFFF_FFFF_FFFF_FFFF, (row >> 64) & 0xFFFF_FFFF_FFFF_FFFF


def _random_rows(count: int, fields_per_row: int, width: int, rng: random.Random) -> list[list[int]]:
    low, high = -(1 << (width - 1)), (1 << (width - 1)) - 1
    # the extremes matter: a zero-extending field select passes the negative
    # ones as large positives, which only a signed compare notices
    return [[rng.choice([low, high, rng.randint(low, high)]) for _ in range(fields_per_row)] for _ in range(count)]


async def _drive(dut, rows: list[list[int]], width: int) -> list[list[int]]:
    fields_per_row = ROW_BITS // width
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.output_ready.value = 1
    dut.status_ready.value = 1
    for _ in range(4):
        await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)

    collected: list[int] = []

    async def collect() -> None:
        while True:
            await RisingEdge(dut.clk)
            await ReadOnly()
            if dut.output_valid.value == 1 and dut.output_ready.value == 1:
                collected.append(int(dut.output_data.value))

    collector = cocotb.start_soon(collect())

    words = [word for fields in rows for word in _pack(fields, width)]
    for index, word in enumerate(words):
        dut.input_data.value = word
        dut.input_last.value = int(index == len(words) - 1)
        dut.input_valid.value = 1
        # the tile's handshake idiom: sample ready in ReadOnly, then leave
        # that phase before driving again
        while True:
            await ReadOnly()
            fired = int(dut.input_ready.value) == 1
            await RisingEdge(dut.clk)
            if fired:
                break
    dut.input_valid.value = 0
    dut.input_last.value = 0
    for _ in range(200):
        await RisingEdge(dut.clk)
    collector.kill()

    out: list[list[int]] = []
    for base in range(0, len(collected) - 1, 2):
        row = collected[base] | (collected[base + 1] << 64)
        out.append([_signed((row >> (i * width)) & ((1 << width) - 1), width) for i in range(fields_per_row)])
    return out


@cocotb.test()
async def width_parameterized_filter_matches_golden(dut) -> None:
    width = _field_width()
    fields_per_row = ROW_BITS // width
    rng = random.Random(7)
    rows = _random_rows(24, fields_per_row, width, rng)

    # Select the LAST field, not the first. Field 0 sits at offset zero for
    # every width, so a predicate reading it cannot tell a correct
    # `sel * FIELD_WIDTH` slice from a stride hardcoded to 32 -- the bench
    # would pass while the tile was width-broken. The highest index puts the
    # stride under maximum load.
    #
    # The comparison is >= 0, which is what a zero-extending field select
    # gets wrong: it reads every negative field as a large positive.
    selected = fields_per_row - 1
    threshold = 0
    dut.cfg_field0.value = selected
    dut.cfg_op0.value = _OP_GE
    dut.cfg_constant0.value = threshold & ((1 << width) - 1)
    dut.cfg_combine01.value = _COMBINE_SINGLE
    dut.cfg_field1.value = 0
    dut.cfg_op1.value = _OP_LT
    dut.cfg_constant1.value = 0
    dut.cfg_combine2.value = _COMBINE_SINGLE
    dut.cfg_field2.value = 0
    dut.cfg_op2.value = _OP_LT
    dut.cfg_constant2.value = 0

    got = await _drive(dut, rows, width)
    want = [row for row in rows if row[selected] >= threshold]
    assert got == want, f"FIELD_WIDTH={width}: device {got[:4]} != golden {want[:4]} ({len(got)} vs {len(want)} rows)"
    assert want != rows, "the predicate must actually reject something, or this proves nothing"

"""Hash-mode grouped field-sum aggregation tile against its golden: one
record per DISTINCT key with input in any order, a signed-key-ascending
drain, overflow as a defined close-out, and fresh state per batch — plus
the randomized-delay patience harness that binds every stateful tile."""

from __future__ import annotations

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import HashGroupedErrorCode, hash_grouped_field_sum_rows

CAPACITY = int(os.environ.get("DAU_HASH_GROUPED_CAPACITY", "512"))

_MASK32 = 0xFFFF_FFFF

# keys deliberately INTERLEAVED and repeated — the ordered tile would open a
# second partial for every reappearance; this tile must not
_ROWS = [
    (5, 1000, 5, 10),
    (-3, 2000, 6, 30),
    (5, -1500, 7, 23),
    (9, -500, 6, 24),
    (-3, 3000, 4, 1),
    (5, 1, 2, 3),
]

_MASK = 0b1010  # SUM(value) + SUM(aux1) per group


@cocotb.test()
async def hash_grouped_matches_golden(dut):
    await _start(dut)
    words, code = await _run(dut, _ROWS, field_mask=_MASK)
    expected, expected_code = hash_grouped_field_sum_rows(_ROWS, field_mask=_MASK, capacity=CAPACITY)
    assert code is expected_code is HashGroupedErrorCode.OK
    assert words == _record_words(expected)
    assert len(expected) == 3  # one record per distinct key, not per run


@cocotb.test()
async def hash_grouped_is_permutation_invariant(dut):
    """The property the ordered tile cannot have: the answer does not
    depend on arrival order."""
    await _start(dut)
    reference, _ = await _run(dut, _ROWS, field_mask=_MASK)
    rng = random.Random(7)
    for _ in range(3):
        shuffled = _ROWS[:]
        rng.shuffle(shuffled)
        words, code = await _run(dut, shuffled, field_mask=_MASK)
        assert code is HashGroupedErrorCode.OK
        assert words == reference


@cocotb.test()
async def hash_grouped_drain_is_signed_key_ascending(dut):
    await _start(dut)
    rows = [(key, key * 10, 0, 0) for key in (9, -9, 0, 2147483647, -2147483648, 3)]
    words, code = await _run(dut, rows, field_mask=0b0010)
    expected, _ = hash_grouped_field_sum_rows(rows, field_mask=0b0010, capacity=CAPACITY)
    assert code is HashGroupedErrorCode.OK
    assert [group.key for group in expected] == [-2147483648, -9, 0, 3, 9, 2147483647]
    assert words == _record_words(expected)


@cocotb.test()
async def hash_grouped_all_four_fields(dut):
    await _start(dut)
    rows = [(2, 5, 7, 11), (-4, 13, 17, 19), (2, -23, 29, -31)]
    words, code = await _run(dut, rows, field_mask=0b1111)
    expected, _ = hash_grouped_field_sum_rows(rows, field_mask=0b1111, capacity=CAPACITY)
    assert code is HashGroupedErrorCode.OK
    assert words == _record_words(expected)
    # summing field 0 is the key itself: key * count
    assert expected[1].sums[0] == 2 * 2


@cocotb.test()
async def hash_grouped_colliding_keys_probe_correctly(dut):
    """Keys congruent modulo CAPACITY share a hash slot; the linear probe
    must keep them distinct."""
    await _start(dut)
    rows = [(0, 1, 0, 0), (CAPACITY, 2, 0, 0), (2 * CAPACITY, 4, 0, 0), (CAPACITY, 8, 0, 0)]
    words, code = await _run(dut, rows, field_mask=0b0010)
    expected, expected_code = hash_grouped_field_sum_rows(rows, field_mask=0b0010, capacity=CAPACITY)
    assert code is expected_code
    assert words == _record_words(expected)


@cocotb.test()
async def hash_grouped_overflow_is_a_defined_close_out(dut):
    """A new key with the table full invalidates the batch — never an
    eviction, never a silent drop."""
    await _start(dut)
    rows = [(key, 1, 0, 0) for key in range(CAPACITY + 1)]
    words, code = await _run(dut, rows, field_mask=0b0010, expect_error=True)
    assert code is HashGroupedErrorCode.GROUP_CAPACITY_EXCEEDED
    assert words == []  # an error close-out invalidates the batch's records
    assert int(dut.group_count.value) == 0


@cocotb.test()
async def hash_grouped_full_table_without_a_new_key_succeeds(dut):
    await _start(dut)
    rows = [(key, 1, 0, 0) for key in range(CAPACITY)] + [(0, 5, 0, 0)]
    words, code = await _run(dut, rows, field_mask=0b0010)
    expected, _ = hash_grouped_field_sum_rows(rows, field_mask=0b0010, capacity=CAPACITY)
    assert code is HashGroupedErrorCode.OK
    assert words == _record_words(expected)


@cocotb.test()
async def hash_grouped_state_is_fresh_per_batch(dut):
    await _start(dut)
    first, _ = await _run(dut, _ROWS, field_mask=_MASK)
    second, code = await _run(dut, [(5, 7, 0, 0)], field_mask=_MASK)
    expected, _ = hash_grouped_field_sum_rows([(5, 7, 0, 0)], field_mask=_MASK, capacity=CAPACITY)
    assert code is HashGroupedErrorCode.OK
    assert second == _record_words(expected)  # not accumulated onto batch one
    assert await _run(dut, _ROWS, field_mask=_MASK) == (first, HashGroupedErrorCode.OK)


@cocotb.test()
async def hash_grouped_bad_configuration_is_refused(dut):
    await _start(dut)
    words, code = await _run(dut, _ROWS, field_mask=0, expect_error=True)
    assert code is HashGroupedErrorCode.BAD_FIELD_MASK and words == []


@cocotb.test()
async def hash_grouped_torn_row_is_refused(dut):
    await _start(dut)
    dut.cfg_field_mask.value = _MASK
    send = cocotb.start_soon(_send_words(dut, [0x0000_0001_0000_0005]))  # w0 with last
    words = await _collect_output(dut, expect_error=True)
    await send
    error, code = await _accept_status(dut)
    assert error and HashGroupedErrorCode(code) is HashGroupedErrorCode.BAD_STREAM
    assert words == []


@cocotb.test()
async def hash_grouped_is_patient(dut):
    """The binding LID requirement: randomized relay-station delays on
    every channel change NOTHING about the emitted records."""
    await _start(dut)
    rng = random.Random(11)
    # distinct keys must fit the table: the patience property is about
    # DELAYS changing nothing, not about the overflow path
    span = min(6, (CAPACITY - 1) // 2)  # 2*span+1 distinct keys must fit
    rows = [(rng.randint(-span, span), rng.randint(-500, 500), rng.randint(0, 9), rng.randint(-9, 9)) for _ in range(40)]
    expected, expected_code = hash_grouped_field_sum_rows(rows, field_mask=0b1110, capacity=CAPACITY)
    assert expected_code is HashGroupedErrorCode.OK
    for seed in (1, 2, 3):
        words, code = await _run(dut, rows, field_mask=0b1110, delay_seed=seed)
        assert code is HashGroupedErrorCode.OK, seed
        assert words == _record_words(expected), seed


def _record_words(groups) -> list[int]:
    words: list[int] = []
    for group in groups:
        words.extend(group.record_words())
    return words


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.cfg_field_mask.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, rows, *, field_mask: int, delay_seed=None, expect_error: bool = False):
    dut.cfg_field_mask.value = field_mask
    gap = random.Random(delay_seed).randint if delay_seed is not None else None
    stall = random.Random((delay_seed or 0) + 100).randint if delay_seed is not None else None
    send = cocotb.start_soon(_send_words(dut, _pack_rows(rows), gap=gap))
    words = await _collect_output(dut, stall=stall, expect_error=expect_error)
    await send
    error, code = await _accept_status(dut)
    return words, HashGroupedErrorCode(code) if error else HashGroupedErrorCode.OK


def _pack_rows(rows) -> list[int]:
    words = []
    for key, value, aux0, aux1 in rows:
        words.append(((value & _MASK32) << 32) | (key & _MASK32))
        words.append(((aux1 & _MASK32) << 32) | (aux0 & _MASK32))
    return words


async def _send_words(dut, words, *, gap=None) -> None:
    for index, word in enumerate(words):
        if gap is not None:
            for _ in range(gap(0, 4)):
                dut.input_valid.value = 0
                await RisingEdge(dut.clk)
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


async def _collect_output(dut, *, stall=None, expect_error: bool = False) -> list[int]:
    words: list[int] = []
    held = None
    budget = 8 * CAPACITY * (CAPACITY + 8) + 65536  # the O(C x live) drain
    for _ in range(budget):
        ready = 1
        if stall is not None and stall(0, 4) == 0:
            ready = 0
        dut.output_ready.value = ready
        await ReadOnly()
        valid = int(dut.output_valid.value) == 1
        fired = ready == 1 and valid
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        status_pending = int(dut.status_valid.value) == 1
        if held is not None:
            # valid/ready hold rule: a stalled beat stays asserted and stable
            assert valid, "output_valid dropped while a beat was stalled"
            assert (word, last) == held, "output beat changed while stalled"
        held = None if (fired or not valid) else (word, last)
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                dut.output_ready.value = 0
                return words
        elif status_pending and expect_error:
            dut.output_ready.value = 0
            return words
    raise AssertionError("timed out collecting group records")


async def _accept_status(dut):
    dut.status_ready.value = 1
    for _ in range(8 * CAPACITY + 512):
        if int(dut.status_valid.value) == 1:
            error = int(dut.status_error.value) == 1
            code = int(dut.status_error_code.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return (error, code)
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for close-out")

from __future__ import annotations

import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import DedupErrorCode, count_distinct_rows

_MASK32 = 0xFFFF_FFFF
_MASK64 = 0xFFFF_FFFF_FFFF_FFFF
_INT32_MIN = -(1 << 31)
_INT32_MAX = (1 << 31) - 1

# matches the DUT parameter default
_KEY_SPACE = 4096


def _rows(keys):
    """Quad rows carrying `keys`. The value and aux fields are deliberately
    varied junk: a dedup reads w0.lo and nothing else, so rows that differ in
    every other field are still duplicates."""
    return [(key, 7000 + index, -index, index * 3) for index, key in enumerate(keys)]


@cocotb.test()
async def all_distinct_keys_are_all_counted(dut):
    await _start(dut)
    rows = _rows([5, 9, 100, 4095, 0, 63, 64])
    assert await _run_batch(dut, rows) == len(rows) == _golden(rows).count


@cocotb.test()
async def one_key_repeated_counts_once(dut):
    await _start(dut)
    rows = _rows([77] * 12)
    assert await _run_batch(dut, rows) == 1 == _golden(rows).count


@cocotb.test()
async def consecutive_duplicate_keys_are_counted_once(dut):
    # THE READ-DURING-WRITE CASE. Row N's bitmap write lands in the same cycle
    # row N+1 issues its read, so a duplicate key reads the pre-insert word
    # unless the write is forwarded. Without the forward each of these counts
    # twice -- and silently, since nothing else about the batch looks wrong.
    await _start(dut)
    for keys in ([7, 7], [7, 7, 7, 7, 7], [7, 7, 100, 100, 7, 7], [3, 3, 3, 9, 9, 3]):
        rows = _rows(keys)
        assert await _run_batch(dut, rows) == _golden(rows).count, keys


@cocotb.test()
async def neighbouring_keys_in_one_word_keep_every_bit(dut):
    # The other half of the hazard, and the one a count alone can hide: two
    # keys 64 apart or less share a bitmap WORD, so a stale read forwards the
    # wrong word and the earlier key's bit is dropped when the later key
    # writes back. Interleaving the same word's keys makes that lost bit
    # visible as a recount later in the batch.
    await _start(dut)
    for keys in ([0, 1, 0, 1], [0, 1, 2, 3, 0, 1, 2, 3], list(range(64)) + list(range(64))):
        rows = _rows(keys)
        assert await _run_batch(dut, rows) == _golden(rows).count, keys[:8]


@cocotb.test()
async def a_second_batch_continues_the_accumulation(dut):
    # The bitmap survives a batch boundary: a scan split into batches is ONE
    # dedup, so keys already seen must not be counted again and the running
    # count carries forward. This is the property that makes overlapped
    # batches summable at all -- per-batch counts are not.
    await _start(dut)
    first_rows = _rows([1, 2, 3, 2, 1])
    second_rows = _rows([3, 4, 1, 5])
    first = _golden(first_rows)
    second = count_distinct_rows(second_rows, key_space=_KEY_SPACE, seen=first.seen)

    assert await _run_batch(dut, first_rows, clear=True) == first.count == 3
    assert await _run_batch(dut, second_rows, clear=False) == second.count == 5
    # a third batch of keys already seen adds nothing
    assert await _run_batch(dut, _rows([1, 2, 3, 4, 5]), clear=False) == 5


@cocotb.test()
async def cfg_clear_starts_a_fresh_accumulation(dut):
    await _start(dut)
    rows = _rows([1, 2, 3])
    assert await _run_batch(dut, rows, clear=True) == 3
    assert await _run_batch(dut, rows, clear=False) == 3  # same keys: nothing new
    assert await _run_batch(dut, rows, clear=True) == 3  # swept: counted again from empty
    assert await _run_batch(dut, _rows([9]), clear=False) == 4


@cocotb.test()
async def the_count_port_reports_the_emitted_record(dut):
    await _start(dut)
    rows = _rows([4, 4, 8, 15, 16, 23, 42, 8])
    count = await _run_batch(dut, rows)
    assert count == _golden(rows).count == 6
    assert int(dut.distinct_count.value) == count


@cocotb.test()
async def out_of_range_keys_report_range_error(dut):
    # A dedup has nowhere to record an out-of-range key, and wrapping it would
    # merge two distinct keys into one count -- so unlike the membership
    # filter, where an unrepresentable key is honestly a non-member, this is an
    # error. The batch drains and the accumulation goes with it.
    await _start(dut)
    for key in (_KEY_SPACE, -1, _INT32_MIN, _INT32_MAX):
        rows = _rows([1, 2, key, 3])
        assert _golden(rows).error is DedupErrorCode.RANGE, key
        dut.cfg_clear.value = 1
        send = cocotb.start_soon(_send_rows(dut, rows))
        await _expect_status(dut, error=True, code=DedupErrorCode.RANGE)
        await send
        assert int(dut.distinct_count.value) == 0, key
        assert int(dut.output_valid.value) == 0, "an invalid batch must emit no count record"
    # recovers, and from an EMPTY bitmap: the failed batch's inserts are gone
    assert await _run_batch(dut, _rows([1, 2, 3]), clear=False) == 3


@cocotb.test()
async def a_torn_row_reports_stream_error(dut):
    await _start(dut)
    dut.cfg_clear.value = 1
    await _send_words(dut, [(500 << 32) | 7])  # input_last on a w0
    await _expect_status(dut, error=True, code=DedupErrorCode.STREAM)
    assert int(dut.distinct_count.value) == 0
    # mid-batch: two whole rows, then a bare w0 carrying last
    dut.cfg_clear.value = 1
    await _send_words(dut, _pack_rows(_rows([1, 2])) + [(500 << 32) | 3])
    await _expect_status(dut, error=True, code=DedupErrorCode.STREAM)
    assert int(dut.distinct_count.value) == 0
    assert await _run_batch(dut, _rows([1, 2, 3]), clear=False) == 3


@cocotb.test()
async def backpressure_on_the_count_word_and_gaps_in_the_input(dut):
    # output backpressure holds the count word; input gaps stretch the two
    # cycles a row takes, which is where the insert pipeline could lose a row
    await _start(dut)
    rows = _rows([1, 1, 2, 3, 3, 3, 4])
    for pattern in ((1, 0, 0, 0, 1), (0, 0, 1), (1,)):
        for gaps in (0, 1, 3):
            assert await _run_batch(dut, rows, ready_pattern=pattern, gaps=gaps) == _golden(rows).count, (pattern, gaps)


@cocotb.test()
async def success_status_survives_backpressure(dut):
    # the close-out must hold stable while status_ready is low, and the tile
    # must refuse new input until the handshake completes
    await _start(dut)
    dut.cfg_clear.value = 1
    dut.status_ready.value = 0
    dut.output_ready.value = 1
    await _send_rows(dut, _rows([1, 2, 2]))

    found = False
    for _ in range(16):
        await ReadOnly()
        found = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if found:
            break
    assert found, "success status never asserted"
    for _ in range(6):
        await ReadOnly()
        assert int(dut.status_valid.value) == 1, "success status not held"
        assert int(dut.status_error.value) == 0
        assert int(dut.input_ready.value) == 0, "input accepted while a close-out was pending"
        await RisingEdge(dut.clk)

    dut.output_ready.value = 0
    await _expect_status(dut, error=False)
    await _assert_quiescent(dut)
    assert int(dut.distinct_count.value) == 2


@cocotb.test()
async def constrained_random_batches_match_golden(dut):
    await _start(dut)
    rng = random.Random(0x0DEDD)
    seen = frozenset()
    for trial in range(40):
        clear = rng.random() < 0.4
        # a small key pool so duplicates -- and same-word neighbours -- are the
        # norm rather than the exception, which is the dedup regime
        pool = [rng.randrange(_KEY_SPACE) for _ in range(rng.randint(1, 6))] + [rng.randrange(64) for _ in range(3)]
        keys = [rng.choice(pool) for _ in range(rng.randint(1, 14))]
        rows = _rows(keys)
        expected = count_distinct_rows(rows, key_space=_KEY_SPACE, seen=() if clear else seen)
        pattern = tuple(rng.choice((0, 1)) for _ in range(rng.randint(1, 5)))
        if not any(pattern):
            pattern = pattern + (1,)
        count = await _run_batch(dut, rows, clear=clear, ready_pattern=pattern, gaps=rng.randint(0, 2))
        assert count == expected.count, f"trial {trial}: clear={clear} keys={keys}"
        seen = expected.seen


def _golden(rows):
    return count_distinct_rows(rows, key_space=_KEY_SPACE)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.cfg_clear.value = 0
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


def _pack_rows(rows) -> list[int]:
    words = []
    for key, value, aux0, aux1 in rows:
        words.append(((value & _MASK32) << 32) | (key & _MASK32))
        words.append(((aux1 & _MASK32) << 32) | (aux0 & _MASK32))
    return words


async def _send_words(dut, words, *, gaps: int = 0) -> None:
    for index, word in enumerate(words):
        for _ in range(gaps):
            dut.input_valid.value = 0
            await RisingEdge(dut.clk)
        dut.input_data.value = word & _MASK64
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


async def _send_rows(dut, rows, *, gaps: int = 0) -> None:
    await _send_words(dut, _pack_rows(rows), gaps=gaps)


async def _run_batch(dut, rows, *, clear: bool = True, ready_pattern=(1,), gaps: int = 0) -> int:
    dut.cfg_clear.value = int(clear)
    send = cocotb.start_soon(_send_rows(dut, rows, gaps=gaps))
    count = await _collect_count(dut, ready_pattern)
    await send
    return count


async def _collect_count(dut, ready_pattern) -> int:
    """Consume the batch's one-word count record, then its success close-out."""
    held = None
    for index in range(200000):
        ready = ready_pattern[index % len(ready_pattern)]
        dut.output_ready.value = ready
        await ReadOnly()
        valid = int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        assert not (int(dut.status_valid.value) == 1 and valid), "the close-out overtook the count word it must follow"
        fired = ready == 1 and valid
        if held is not None:
            assert valid, "output_valid dropped while the count word was stalled"
            assert (word, last) == held, "the count word changed while stalled"
        held = None if (fired or not valid) else (word, last)
        await RisingEdge(dut.clk)
        if fired:
            dut.output_ready.value = 0
            assert last == 1, "the count record is one word and must carry output_last"
            await _expect_status(dut, error=False)
            await _assert_quiescent(dut)
            return word
    raise AssertionError("timed out waiting for the dedup count word")


async def _assert_quiescent(dut) -> None:
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
    for _ in range(1 << 20):
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
    raise AssertionError("timed out waiting for the dedup status")

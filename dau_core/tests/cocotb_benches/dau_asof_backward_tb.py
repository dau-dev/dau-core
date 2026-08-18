from __future__ import annotations

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ReadOnly, RisingEdge

from dau_core.operators import keyed_asof_join_rows

# (symbol, time, bid, ask)
_QUOTES = [
    (570, 100, 10, 12),
    (570, 105, 11, 13),
    (570, 120, 9, 11),
    (571, 100, 50, 52),
    (571, 110, 51, 54),
]
# (symbol, time, price, size)
_TRADES = [
    (570, 99, 11, 5),  # before the first quote -> miss
    (570, 100, 12, 3),  # equal time: takes the 100 quote (inclusive)
    (570, 107, 13, 2),  # between 105 and 120: the 105 quote
    (570, 130, 8, 1),  # after the last quote: the 120 quote
    (571, 105, 53, 7),  # the 100 quote
    (571, 111, 55, 4),  # the 110 quote
    (572, 100, 1, 1),  # a symbol with no quotes at all -> miss
]


def _merge(quotes, trades):
    """Host pre-merge: one (symbol, time)-sorted stream, quote-before-trade at
    equal (symbol, time). Records are (is_trade, symbol, time, a, b)."""
    tagged = [(0, s, t, bid, ask) for (s, t, bid, ask) in quotes]
    tagged += [(1, s, t, price, size) for (s, t, price, size) in trades]
    tagged.sort(key=lambda r: (r[1], r[2], r[0]))  # symbol, time, quote(0) before trade(1)
    return tagged


def _expected(quotes, trades, *, tolerance=None):
    """The tile's golden is the generic keyed as-of join over the merged stream
    (quote = build {bid, ask}, trade = probe {price, size}) — the joined row
    (key, time, probe_a, probe_b, build_a, build_b) is the market row the bench
    decodes."""
    return keyed_asof_join_rows(_merge(quotes, trades), tolerance=tolerance)


@cocotb.test()
async def asof_tile_matches_golden(dut):
    await _start(dut)
    rows = await _run(dut, _merge(_QUOTES, _TRADES))
    assert rows == _expected(_QUOTES, _TRADES)


@cocotb.test()
async def asof_tile_backpressure_both_sides(dut):
    await _start(dut)
    rows = await _run(dut, _merge(_QUOTES, _TRADES), ready_pattern=(1, 0, 0, 1, 0))
    assert rows == _expected(_QUOTES, _TRADES)


@cocotb.test()
async def asof_tile_tolerance_drops_stale_quotes(dut):
    await _start(dut)
    tol = 10
    rows = await _run(dut, _merge(_QUOTES, _TRADES), tolerance=tol)
    assert rows == _expected(_QUOTES, _TRADES, tolerance=tol)


@cocotb.test()
async def asof_tile_all_misses_emit_nothing(dut):
    await _start(dut)
    trades = [(9, 5, 1, 1), (9, 6, 2, 2)]  # no build (quote) for key 9
    rows = await _run(dut, _merge([], trades))
    assert rows == []
    assert keyed_asof_join_rows(_merge([], trades)) == []


@cocotb.test()
async def asof_tile_int64_time_axis(dut):
    await _start(dut)
    big = 1 << 40
    quotes = [(3, big, 7, 9)]
    trades = [(3, big + 5, 8, 2)]
    rows = await _run(dut, _merge(quotes, trades))
    assert rows == _expected(quotes, trades)
    assert rows == [(3, big + 5, 8, 2, 7, 9)]


@cocotb.test()
async def asof_tile_reappearing_symbol_resets_quote(dut):
    await _start(dut)
    # 570 appears, then 571, then 570 again: the second 570 group has no held
    # quote carried from the first, so its early trade is a miss
    quotes = [(570, 10, 1, 2), (571, 10, 3, 4), (570, 40, 5, 6)]
    trades = [(570, 12, 9, 1), (571, 12, 9, 1), (570, 20, 9, 1), (570, 45, 9, 1)]
    rows = await _run(dut, _merge(quotes, trades))
    assert rows == _expected(quotes, trades)


@cocotb.test()
async def asof_tile_marks_output_last_on_final_row_after_trailing_miss(dut):
    await _start(dut)
    quotes = [(570, 10, 1, 2)]
    # two matches then a trailing miss (unmatched trade on a fresh symbol): the
    # final MATCH is not the final RECORD, so output_last must ride the buffered
    # last matched row, not the record that happens to end the stream
    trades = [(570, 12, 9, 1), (570, 15, 9, 1), (999, 20, 9, 1)]
    dut.cfg_has_tolerance.value = 0
    dut.cfg_tolerance.value = 0
    send = cocotb.start_soon(_send_records(dut, _merge(quotes, trades)))
    words, last_at = await _collect_with_last(dut)
    await send
    count = await _accept_success_status(dut)
    rows = [_decode_row(words[offset : offset + 4]) for offset in range(0, len(words), 4)]
    assert rows == _expected(quotes, trades)
    assert count == len(rows)
    assert last_at == len(words) - 1, (last_at, len(words))  # last on the final beat


@cocotb.test()
async def asof_tile_extreme_timestamp_age_misses(dut):
    await _start(dut)
    int64_min = -(1 << 63)
    int64_max = (1 << 63) - 1
    quotes = [(3, int64_min, 7, 9)]
    trades = [(3, 0, 8, 2)]  # age = 2**63 > INT64_MAX tolerance -> miss (65-bit)
    rows = await _run(dut, _merge(quotes, trades), tolerance=int64_max)
    assert rows == _expected(quotes, trades, tolerance=int64_max)
    assert rows == []


@cocotb.test()
async def asof_tile_truncated_record_reports_stream_error(dut):
    await _start(dut)
    dut.output_ready.value = 1
    # input_last on w0 (the symbol word): a torn 3-word record
    dut.input_data.value = 570
    dut.input_last.value = 1
    dut.input_valid.value = 1
    while True:
        await ReadOnly()
        fired = int(dut.input_ready.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            break
    dut.input_valid.value = 0
    dut.input_last.value = 0
    await _expect_error(dut)


async def _start(dut) -> None:
    clock = Clock(dut.clk, 10, unit="ns")
    cocotb.start_soon(clock.start(start_high=False))
    dut.rst.value = 1
    dut.input_valid.value = 0
    dut.input_data.value = 0
    dut.input_last.value = 0
    dut.output_ready.value = 0
    dut.status_ready.value = 0
    dut.cfg_has_tolerance.value = 0
    dut.cfg_tolerance.value = 0
    await RisingEdge(dut.clk)
    dut.rst.value = 0
    await RisingEdge(dut.clk)


async def _run(dut, records, *, ready_pattern=(1,), tolerance=None):
    dut.cfg_has_tolerance.value = int(tolerance is not None)
    dut.cfg_tolerance.value = (tolerance if tolerance is not None else 0) & ((1 << 64) - 1)
    send = cocotb.start_soon(_send_records(dut, records))
    words = await _collect_output(dut, ready_pattern)
    await send
    count = await _accept_success_status(dut)
    assert len(words) % 4 == 0, "market rows are 4 words"
    assert count == len(words) // 4
    return [_decode_row(words[offset : offset + 4]) for offset in range(0, len(words), 4)]


def _decode_row(words):
    time = _signed64(words[0])
    symbol = _signed32(words[1] & 0xFFFFFFFF)
    price = _signed32(words[1] >> 32)
    bid = _signed32(words[2] & 0xFFFFFFFF)
    ask = _signed32(words[2] >> 32)
    size = _signed32(words[3] & 0xFFFFFFFF)
    return (symbol, time, price, size, bid, ask)


async def _send_records(dut, records) -> None:
    words = []
    for is_trade, symbol, time, a, b in records:
        words.append(((is_trade & 0xFFFFFFFF) << 32) | (symbol & 0xFFFFFFFF))
        words.append(time & ((1 << 64) - 1))
        words.append(((b & 0xFFFFFFFF) << 32) | (a & 0xFFFFFFFF))
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
    # output_last is set only when the FINAL record is a matched trade; a
    # stream ending in a miss drains its rows and then raises status_valid with
    # no last-flagged word, so terminate on either signal.
    words = []
    for pattern_index in range(8192):
        ready = ready_pattern[pattern_index % len(ready_pattern)]
        dut.output_ready.value = ready
        await ReadOnly()
        fired = ready == 1 and int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        status_pending = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                dut.output_ready.value = 0
                return words
        elif status_pending:
            dut.output_ready.value = 0
            return words
    raise AssertionError("timed out collecting as-of output")


async def _collect_with_last(dut):
    # like _collect_output but records the beat index that carried output_last
    words = []
    last_at = None
    for _ in range(8192):
        dut.output_ready.value = 1
        await ReadOnly()
        fired = int(dut.output_valid.value) == 1
        word = int(dut.output_data.value)
        last = int(dut.output_last.value)
        status_pending = int(dut.status_valid.value) == 1
        await RisingEdge(dut.clk)
        if fired:
            words.append(word)
            if last == 1:
                last_at = len(words) - 1
                dut.output_ready.value = 0
                return words, last_at
        elif status_pending:
            dut.output_ready.value = 0
            return words, last_at
    raise AssertionError("timed out collecting as-of output")


async def _accept_success_status(dut) -> int:
    dut.status_ready.value = 1
    for _ in range(128):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 0
            count = int(dut.match_count.value)
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return count
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for success status")


async def _expect_error(dut) -> None:
    dut.status_ready.value = 1
    for _ in range(64):
        if int(dut.status_valid.value) == 1:
            assert int(dut.status_error.value) == 1
            assert int(dut.status_error_code.value) == 2
            await RisingEdge(dut.clk)
            dut.status_ready.value = 0
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timed out waiting for error status")


def _signed32(value: int) -> int:
    return value - (1 << 32) if value >= 1 << 31 else value


def _signed64(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value

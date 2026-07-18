from __future__ import annotations

import cocotb

from dau_core.operators import (
    MembershipBuildSet,
    MembershipErrorCode,
    MembershipKind,
    MembershipMode,
    filter_rows_by_membership,
)
from dau_core.tests.cocotb_benches.dau_int32_key_membership_filter_tb import (
    _drive_cfg,
    _expect_status,
    _load,
    _probe,
    _send_words,
    _start,
)

# KEY_SPACE == 64 is the WORDS == 1 corner: a single-word bitmap. AWIDTH is
# forced to 1, the LOAD phase closes out straight from S_ARM (S_LOAD is never
# entered — the one image word IS the whole image), and every in-range key
# indexes membership_mem[0]. Previously only golden-tested; this bench runs the
# corner in real RTL. The pytest runner builds the DUT with
# parameters={"KEY_SPACE": 64}; the KEY_SPACE-agnostic helpers are shared with
# the KEY_SPACE=4096 bench.
_KEY_SPACE = 64

# (key, value, aux0, aux1); membership key is w0.lo. 64 and -1 are out of range
# at KEY_SPACE=64 -> non-members (the unsigned range compare catches both).
_ROWS = [
    (7, 1000, 5, 10),
    (40, 2000, 6, 30),
    (7, 1500, 7, 23),
    (63, -500, 6, 24),  # high boundary (in range)
    (64, 3000, 4, 1),  # out of range -> non-member
    (-1, 2500, 6, 40),  # negative -> non-member
]


def _build(keys) -> MembershipBuildSet:
    return MembershipBuildSet.from_keys(keys, key_space=_KEY_SPACE)


@cocotb.test()
async def single_word_semi_and_anti_match_golden(dut):
    await _start(dut)
    build = _build({7, 63})
    await _load(dut, build)
    for mode in (MembershipMode.SEMI, MembershipMode.ANTI):
        out = await _probe(dut, _ROWS, mode)
        assert out == filter_rows_by_membership(_ROWS, build, mode=mode), mode.name


@cocotb.test()
async def single_word_load_then_probe_boundaries(dut):
    # the single-word load closes out directly from S_ARM (no S_LOAD state);
    # AWIDTH==1 addressing must still resolve every in-range key to word 0
    await _start(dut)
    build = _build({0, 63})
    await _load(dut, build)
    rows = [
        (0, 1, 1, 1),  # low boundary member
        (63, 2, 2, 2),  # high boundary member
        (64, 3, 3, 3),  # out of range -> non-member
        (-1, 4, 4, 4),  # negative -> non-member
        (1, 5, 5, 5),  # in range, not set -> non-member
    ]
    for mode in (MembershipMode.SEMI, MembershipMode.ANTI):
        assert await _probe(dut, rows, mode) == filter_rows_by_membership(rows, build, mode=mode), mode.name


@cocotb.test()
async def single_word_empty_and_full_sets(dut):
    await _start(dut)
    await _load(dut, _build(set()))
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == []
    assert await _probe(dut, _ROWS, MembershipMode.ANTI) == filter_rows_by_membership(_ROWS, _build(set()), mode=MembershipMode.ANTI)
    full = _build(range(_KEY_SPACE))
    await _load(dut, full)
    # every in-range id is a member; 64 and -1 stay out of range
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == filter_rows_by_membership(_ROWS, full, mode=MembershipMode.SEMI)


@cocotb.test()
async def single_word_over_long_image_reports_load_error(dut):
    # at WORDS==1 the one word is the whole image (S_ARM -> success); a second
    # word makes it over-long (S_ARM -> S_DRAIN_LOAD -> ERR_LOAD)
    await _start(dut)
    _drive_cfg(dut, load=1, mode=MembershipMode.SEMI, kind=MembershipKind.EXACT_BITMAP)
    await _send_words(dut, _build({7}).image_words() + [0])
    await _expect_status(dut, error=True, code=MembershipErrorCode.LOAD)
    # recovers: a well-formed single-word load then probe works
    build = _build({7})
    await _load(dut, build)
    assert await _probe(dut, _ROWS, MembershipMode.SEMI) == filter_rows_by_membership(_ROWS, build, mode=MembershipMode.SEMI)

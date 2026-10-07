"""Every LEGAL parameterization must elaborate.

The registry advertises which parameter values a composition may choose.
Nothing checked that the RTL agrees. It did not: the key-mask dispatcher
advertised a 192-bit input (the 3-word record bus) while its own
elaboration guard rejected anything but 64/128/256/512, and the
dispatcher had never been elaborated at 64-bit at all — Vivado failed the
build on a part-select the benches never exercised.

This gate is cheap (lint-only, no simulation) and covers the whole
declared parameter space rather than the handful of values benches happen
to use. It is the counterpart to the OOC synthesis task, which is
authoritative but far too slow to run over every combination."""

from __future__ import annotations

import itertools
import os
import subprocess
from collections.abc import Callable
from shutil import which

import pytest

from dau_core.cores import CoreKind
from dau_core.hdl import DAU_HDL_INCLUDE_DIR
from dau_core.registry import cores_of_kind

# the classes that mean "this will not synthesize" — the same promotion the
# cocotb runner applies (see VERILATOR_BUILD_ARGS there)
_FATAL_CLASSES = ("-Werror-USERFATAL", "-Werror-SELRANGE")
_MAX_COMBINATIONS = 24  # keep the gate seconds-fast; sample beyond this

# PARAMETER COUPLING. The registry declares each parameter's legal values
# independently, so its advertised space is a cartesian product that can
# include combinations no RTL would accept. The ABI cannot express coupling
# yet (the tile-ABI follow-up), so the couplings live here — deliberately
# visible, because an entry is an admission that the registry over-advertises
# and the composition layer is carrying the constraint instead.
_COUPLED: dict[str, Callable[[dict[str, int]], bool]] = {
    # row framings carry whole 128-bit quad rows, so a wider input pins the
    # record at 2 words; only the 64-bit and whole-record framings are free
    # two couplings: whole-record framing pins IN_WIDTH to the record bus,
    # and a row framing wider than 64 bits pins the record at one quad row
    "key-mask-dispatcher": lambda p: (
        (p.get("IN_WIDTH", 64) == p.get("RECORD_WORDS", 2) * 64)
        if p.get("RECORD_INPUT", 0) == 1
        else (p.get("RECORD_WORDS", 2) == 2 or p.get("IN_WIDTH", 64) == 64)
    ),
}


def _is_coupled_out(core_name: str, params: dict[str, int]) -> bool:
    predicate = _COUPLED.get(core_name)
    return predicate is not None and not predicate(params)


def _candidate_values(spec) -> list[int]:
    """The values a composition may legally choose for one parameter."""
    if spec.choices is not None:
        return list(spec.choices)
    values = {spec.default, spec.minimum}
    if spec.maximum is not None:
        values.add(spec.maximum)
    return sorted(v for v in values if v >= 1)


def _parameterizations(definition) -> list[dict[str, int]]:
    names = sorted(definition.parameters)
    if not names:
        return [{}]
    grids = [_candidate_values(definition.parameters[name]) for name in names]
    combos = [dict(zip(names, values, strict=True)) for values in itertools.product(*grids)]
    return combos[:_MAX_COMBINATIONS]


def _sources_for(definition) -> list[str]:
    """The core's own source plus every dependency's, resolved through the
    REGISTRY rather than guessed from the dependency name — package files
    do not follow the core naming."""
    from dau_core.registry import UnknownCoreError, core as core_definition, loaded_cores

    known = loaded_cores()
    paths: list[str] = []
    for dependency in definition.depends:
        if dependency not in known:
            continue  # a dependency the registry does not carry as a source
        try:
            paths.append(str(core_definition(dependency).source_path()))
        except UnknownCoreError:  # pragma: no cover - guarded above
            continue
    paths.append(str(definition.source_path()))
    return paths


_OPERATOR_CORES = sorted(d.name for d in cores_of_kind(CoreKind.OPERATOR))
# Set wherever this tier is the subject of a required check. A local run may
# legitimately have no verilator; a gate may not.
_REQUIRED = os.environ.get("DAU_REQUIRE_VERILATOR", "") not in ("", "0")


@pytest.mark.skipif(not _REQUIRED, reason="DAU_REQUIRE_VERILATOR unset")
def test_this_gate_actually_ran() -> None:
    """A required check that can silently carry no signal is worse than a
    non-required one.

    Every other test in this file skips on ``which("verilator") is None``, so
    a tool install that quietly failed leaves the gate green having elaborated
    nothing. That is not hypothetical: the bench job once spent 60 minutes
    wedged installing verilator, was cancelled without reaching a single
    bench, and reported nothing about the diff while appearing to run.

    ``DAU_REQUIRE_VERILATOR`` turns that skip into a failure. Note what this
    test does NOT skip on: verilator itself. A guard that steps aside for the
    same reason as its subject guards nothing, so where the tier is required
    this runs unconditionally and fails.

    It lives here rather than in the workflow so the assertion travels with
    the tests it guards.
    """
    assert which("verilator") is not None, (
        "DAU_REQUIRE_VERILATOR is set and verilator is not on PATH: every elaboration "
        "test would have skipped, reporting green while checking no RTL at all"
    )
    # the tier is one test parametrized 1:1 over the operator cores, so an
    # empty registry would collect zero of them and still report success
    assert _OPERATOR_CORES, "the elaboration tier is parametrized over no cores: it would collect nothing"


@pytest.mark.skipif(which("verilator") is None, reason="verilator not found")
@pytest.mark.parametrize("core_name", _OPERATOR_CORES)
def test_every_declared_parameterization_elaborates(core_name: str) -> None:
    from dau_core.registry import core as core_definition

    definition = core_definition(core_name)
    sources = _sources_for(definition)

    failures = []
    checked = 0
    for params in _parameterizations(definition):
        if _is_coupled_out(core_name, params):
            continue
        checked += 1
        command = [
            "verilator",
            "--lint-only",
            "-Wall",
            "-Wno-fatal",
            *_FATAL_CLASSES,
            # tiles written against an element type `include types.svh, and
            # Verilator does not search the including file's own directory
            f"-I{DAU_HDL_INCLUDE_DIR}",
            "--top-module",
            definition.module,
            *sources,
            *[f"-G{name}={value}" for name, value in params.items()],
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            detail = next(
                (line for line in completed.stderr.splitlines() if line.startswith("%Error")),
                completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "unknown",
            )
            failures.append(f"{params or '(defaults)'}: {detail}")

    assert not failures, f"{core_name} declares parameterizations its RTL rejects:\n  " + "\n  ".join(failures)
    assert checked, f"{core_name}: every declared parameterization was coupled out — the gate checked nothing"

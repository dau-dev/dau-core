# dau core

Hardware contracts, golden semantics, and the core registry for the DAU accelerator stack

[![Build Status](https://github.com/dau-dev/dau-core/actions/workflows/build.yaml/badge.svg?branch=main&event=push)](https://github.com/dau-dev/dau-core/actions/workflows/build.yaml)
[![codecov](https://codecov.io/gh/dau-dev/dau-core/branch/main/graph/badge.svg)](https://codecov.io/gh/dau-dev/dau-core)
[![License](https://img.shields.io/github/license/dau-dev/dau-core)](https://github.com/dau-dev/dau-core)
[![PyPI](https://img.shields.io/pypi/v/dau-core.svg)](https://pypi.python.org/pypi/dau-core)

## Overview

`dau-core` is the contract layer of the DAU stack. Everything the other
packages agree on lives here, in three forms:

- **The core registry** — every HDL building block (streaming operator
  tiles, AXI stream infrastructure, shell adapters, shared packages), each
  described by a `CoreDefinition`: its module, sources, dependencies,
  golden model, testbench, measured resource envelope, declared module
  parameters, and streaming-contract facts.
- **Golden semantics** — pure-Python reference implementations for every
  operator. Hardware results byte-compare against these; they are the
  meaning of the tiles.
- **Wire contracts** — register maps, the stream framing structs, DMA
  constraints, capability advertisement, and composition specs, versioned
  and explicit.

The package also ships the canonical SystemVerilog for every registered
core as importable resources, verified by cocotb benches under Verilator.

## The registry is configuration

There is no Python-literal list of cores. The registry is a
[Hydra](https://hydra.cc) config tree (`dau_core/config/dau-core/cores.yaml`,
one `_target_: dau_core.cores.CoreDefinition` entry per core) loaded into
the [ccflow](https://github.com/Point72/ccflow) `ModelRegistry` at
`/dau-core/<core-name>`. The package registers itself on the shared Hydra
search path via a `hydra.lernaplugins` entry point, so any composing
application discovers the cores without importing this package's code.

```python
from dau_core.cores import loaded_cores, core, sources_for

cores = loaded_cores()                     # name -> CoreDefinition, registry order
tile = core("int32-predicate-filter")      # one core
print(tile.kind.value)                     # "operator"
print(tile.golden)                         # "dau_core.operators:filter_values"
print(tile.resources)                      # measured LUT/FF/BRAM/DSP envelope

sources_for("int32-stream-aggregation")    # dependency-closed HDL paths, in order
```

The registered palette today: 27 operator tiles (filters, maps,
aggregations, joins, sorters, partitioners, temporal operators, top-k), 4
stream-infrastructure blocks (AXI burst reader, broadcast, record writer,
write mux), 4 shell adapters, 3 shared SV packages, and 1 simulation
model. Accessors are read-through views: a registry an application has
already composed (including overrides) is the source of truth.

## Golden semantics

Each operator tile's behavior is defined by a pure function or small model
in `dau_core.operators`, `dau_core.temporal`, or `dau_core.aggregations`.
Configuration objects are frozen [pydantic](https://docs.pydantic.dev)
models mirroring the tile's config ports, so the same object drives the
golden, the testbench, and design emission:

```python
from dau_core.operators import (
    PredicateOp, RowField, RowPredicateConfig, RowPredicateTerm,
    filter_rows_by_predicate, streaming_top_k,
)

config = RowPredicateConfig(
    term0=RowPredicateTerm(field=RowField.VALUE, op=PredicateOp.GT, constant=10),
)
rows = [(1, 5, 0, 0), (2, 25, 0, 0), (3, 11, 0, 0)]
filter_rows_by_predicate(rows, config)   # -> [(2, 25, 0, 0), (3, 11, 0, 0)]

streaming_top_k(rows, k=2)               # ORDER BY key DESC LIMIT 2, ties by arrival
```

`dau_core.temporal` covers the time-series operators (backward as-of join,
market features, time/volume/dollar/imbalance bars, rolling moments, EWMA)
in an exact-integer domain: divisions are deferred to the host, so goldens
and hardware agree bit-for-bit.

## Composition specs

A shell datapath is described as data: `ScanCompositionSpec` names
registry cores, their config-port bindings, and module-parameter overrides
for a one-scan-N-lanes design. Construction validates everything against
the registry (operator kinds, status-contract roles, declared parameter
bounds) and raises `CompositionError` before any HDL is generated;
`estimated_resources` prices a spec from the measured envelopes.

```python
from dau_core.composition import LaneSpec, ScanCompositionSpec, TileSpec, estimated_resources

spec = ScanCompositionSpec(
    name="bar-noc-4",
    lanes=tuple(
        LaneSpec(
            tile=TileSpec(core="int32-bar-aggregation", config={"cfg_mode": "2'd0"}),
            partition=TileSpec(core="int32-pair-key-filter",
                               config={"cfg_key_mask": "32'd3", "cfg_key_match": f"32'd{lane}"}),
        )
        for lane in range(4)
    ),
)
print(estimated_resources(spec))
```

## Workflows and typed results

Multi-stage reference workflows are ccflow `CallableModel`s with typed
`ResultBase` results, registrable and Hydra-composable like everything
else. `dau_core.workflows.HeroWorkflow` runs the temporal pipeline
(as-of align → feature map → time bars → rolling moments) over the
goldens and records the cardinality funnel and per-stage materialization
cost model (`dau_core.hero`).

```python
from ccflow import GenericContext
from dau_core.temporal import generate_market_stream
from dau_core.workflows import HeroWorkflow

stream = generate_market_stream(seed=7, num_symbols=3)
result = HeroWorkflow(bar_width=64, roll_window=3)(GenericContext(value=stream))
print(result.funnel)   # input -> matched -> bars -> rolling row counts
```

## Wire contracts

- `dau_core.registers` — the DAU magic word, register/protocol versions,
  register offsets, reset scopes, and the stream-job register contract
  (trigger/status/buffer addresses).
- `dau_core.stream` — Arrow-lite framing (`BatchHeader`,
  `ColumnDescriptor`, `OperationDescriptor`), logical types, operation
  codes, record-batch limits, and shared error codes.
- `dau_core.capabilities` — DMA constraints and device/operator
  capability models populated from a live device.
- `dau_core.capability_contract` — the composed capability advertisement
  a built shell publishes, derived from its composition spec.
- `dau_core.design` — the operator-unit inventory parsed back from a
  synthesized design's manifest.

## HDL and verification

`dau_core/hdl/` holds the canonical SystemVerilog for every registered
core. Tiles speak a common 64-bit valid/ready stream + status contract
(`input_last` frames a batch; a terminal tile closes out with a status
word and count, mid-lane stages are silent on success). Verification runs
three ways, all from `pytest`:

- text-contract checks over the packaged sources;
- standalone Verilator benches;
- cocotb-on-Verilator benches, golden-equal against the registered
  reference for each tile.

## Installation

```bash
pip install dau-core
```

Development install and checks:

```bash
pip install -e .[develop]
python -m pytest dau_core/tests   # HDL benches need verilator on PATH
python -m ruff check dau_core
```

> [!NOTE]
> This library was generated using [copier](https://copier.readthedocs.io/en/stable/) from the [Base Python Project Template repository](https://github.com/python-project-templates/base).

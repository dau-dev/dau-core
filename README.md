# dau core

Hardware contracts, golden semantics, and the core registry for the DAU accelerator stack

[![Build Status](https://github.com/dau-dev/dau-core/actions/workflows/build.yaml/badge.svg?branch=main&event=push)](https://github.com/dau-dev/dau-core/actions/workflows/build.yaml)
[![codecov](https://codecov.io/gh/dau-dev/dau-core/branch/main/graph/badge.svg)](https://codecov.io/gh/dau-dev/dau-core)
[![License](https://img.shields.io/github/license/dau-dev/dau-core)](https://github.com/dau-dev/dau-core)
[![PyPI](https://img.shields.io/pypi/v/dau-core.svg)](https://pypi.python.org/pypi/dau-core)

## Overview

`dau-core` holds the definitions the other DAU packages share:

- **The core registry.** Every HDL building block (streaming operator
  tiles, AXI stream infrastructure, shell adapters, shared packages) is
  described by a `CoreDefinition`: its module, sources, dependencies,
  golden model, testbench, measured resources, module parameters, and
  stream-contract facts.
- **Golden models.** Each operator has a pure-Python reference
  implementation. Hardware results are compared byte for byte against it,
  so the golden defines what the tile computes.
- **Wire contracts.** Register maps, stream framing structs, DMA
  constraints, capability advertisement, and composition specs.

The package also ships the SystemVerilog for every registered core.
Every operator tile has a golden, a cocotb bench under Verilator, and
measured resource points. The stream infrastructure and shell adapters
are tested inside the composed shells instead of one by one. Five tiles
generate their source from their parameters when rendered.

## Documentation

The documentation is in [the wiki](https://github.com/dau-dev/dau-core/wiki),
written under [`docs/wiki`](docs/wiki).

- **Tutorial**: [read a tile's contract end to end](https://github.com/dau-dev/dau-core/wiki/Read-a-Tile-Contract)
  follows one operator through the five places its definition lives.
- **How-to guides**: [add an operator tile](https://github.com/dau-dev/dau-core/wiki/Add-an-Operator-Tile),
  [test a core](https://github.com/dau-dev/dau-core/wiki/Test-a-Core), and
  [measure a resource envelope](https://github.com/dau-dev/dau-core/wiki/Measure-a-Resource-Envelope),
  the one step that needs a synthesis host.
- **Reference**: the [`CoreDefinition` fields](https://github.com/dau-dev/dau-core/wiki/Core-Definition)
  and the [registry-wide guards](https://github.com/dau-dev/dau-core/wiki/Registry-Guards)
  with what each one checks.
- **Explanation**: [why cores are declared in config](https://github.com/dau-dev/dau-core/wiki/Why-Cores-Are-Declared-in-Config),
  [why each tile has a golden model](https://github.com/dau-dev/dau-core/wiki/Why-Each-Tile-Has-a-Golden-Model),
  and [why resource costs are measured](https://github.com/dau-dev/dau-core/wiki/Why-Resources-Are-Measured).

## The core registry

Cores are declared in a [Hydra](https://hydra.cc) config tree,
`dau_core/config/dau-core/cores.yaml`, with one `CoreDefinition` entry per
core, and loaded into the [ccflow](https://github.com/Point72/ccflow)
`ModelRegistry` at `/dau-core/<core-name>`. The package adds the tree to the
shared Hydra search path through a `hydra.lernaplugins` entry point, so an
application that composes Hydra configs finds the cores without importing
`dau_core`.

```python
from dau_core.registry import loaded_cores, core, sources_for

cores = loaded_cores()                     # name -> CoreDefinition, registry order
tile = core("int32-predicate-filter")      # one core
print(tile.kind.value)                     # "operator"
print(tile.golden)                         # "dau_core.operators:filter_values"
print(tile.resources)                      # measured LUT/FF/BRAM/DSP envelope

sources_for("stream-aggregation")    # dependency-closed HDL paths, in order
```

The palette has 46 operator tiles (filters, maps, ordered and hash-mode
aggregations, joins, membership, sorters, partitioners, dispatchers,
temporal operators, top-k, fixed-point transcendentals, and the stateful
group), 5 stream-infrastructure blocks (AXI burst reader, broadcast, record
writer, write mux, row unpacker), 4 shell adapters, 3 shared SV packages,
and 1 simulation model: 59 entries in all. `loaded_cores()` returns the
current list. If an application has already composed the registry, with or
without overrides, the accessors read that composition instead of loading
the packaged defaults.

## Golden models

Each operator tile's behavior is defined by a pure function or small model
in `dau_core.operators` or `dau_core.temporal`. Tile configurations are
frozen [pydantic](https://docs.pydantic.dev) models that mirror the tile's
config ports, and the same object drives the golden, the testbench, and
design emission. Their mapping fields are read-only, `replace()` is a
`model_copy` that validates the result, and `check()` validates a model
again on demand, so a copy that skipped validation is rejected before
anything hashes or prices it.

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
            tile=TileSpec(core="bar-aggregation", config={"cfg_mode": "2'd0"}),
            partition=TileSpec(core="pair-key-filter",
                               config={"cfg_key_mask": "32'd3", "cfg_key_match": f"32'd{lane}"}),
        )
        for lane in range(4)
    ),
)
print(estimated_resources(spec, part="xc7a200tfbg484-2", clock_ns=8.0))
```

Pricing needs a part and a clock. Each core is looked up at exactly that
part, clock, and parameter set, and a combination that has not been
measured raises `UnmeasuredResourceError` naming it. Nothing is
interpolated.

### Wide lanes

A quad-row operator core takes one row per beat at the rate its `ingest`
contract declares (one row per cycle or per two cycles, depending on the
tile). A `wide-row-stream` core takes the reader's whole beat,
`SLOTS = data_width / 128` quad rows every cycle, with no dispatcher and no
fan-out. The three row operators have wide counterparts
(`wide-row-predicate-filter`, `wide-row-map-alu`,
`wide-field-sum-aggregation`) that take the same configs in the same order;
only the core names change.

`data_width` must equal `SLOTS * 128` or construction raises
`CompositionError`. A wide lane cannot use the packed front unpacker,
because packing saves DMA bytes at the cost of job time and a wide lane
needs whole rows.

```python
from dau_core.composition import LaneSpec, ScanCompositionSpec, TileSpec, estimated_resources

spec = ScanCompositionSpec(
    name="q6-wide",
    data_width=512,   # SLOTS=4: four quad rows per cycle
    lanes=(
        LaneSpec(
            tile=TileSpec(core="wide-field-sum-aggregation",
                          config={"cfg_field_mask": "4'b0010"}),
            chain=(
                # shipdate >= 8766 AND shipdate < 9131
                TileSpec(core="wide-row-predicate-filter", config={
                    "cfg_field0": "2'd0", "cfg_op0": "3'd6", "cfg_constant0": "32'd8766",
                    "cfg_combine01": "2'd1",
                    "cfg_field1": "2'd0", "cfg_op1": "3'd3", "cfg_constant1": "32'd9131",
                    "cfg_combine2": "2'd0",
                    "cfg_field2": "2'd0", "cfg_op2": "3'd0", "cfg_constant2": "32'd0"}),
                # value = value * aux0 >> 15
                TileSpec(core="wide-row-map-alu", config={
                    "cfg_op": "3'd4", "cfg_dst_field": "2'd1",
                    "cfg_src_a_field": "2'd1", "cfg_src_b_field": "2'd2",
                    "cfg_shift": "5'd15"}),
            ),
        ),
    ),
)
print(estimated_resources(spec, part="xc7k325tffg900-2", clock_ns=8.0))
```

The rate model predicts rows / SLOTS / clock. At SF1 that is
6,001,215 / 4 / 125 MHz = 12.0 ms, and the job measures 12.4 ms on
hardware.

## Workflows and typed results

Multi-stage reference workflows are ccflow `CallableModel`s with typed
`ResultBase` results, so they can be registered and composed with Hydra
like the rest of the package. `dau_core.workflows.HeroWorkflow` runs the
temporal pipeline (as-of align, feature map, time bars, rolling moments)
over the goldens and records the row count at each stage and a per-stage
materialization cost (`dau_core.hero`).

```python
from ccflow import GenericContext
from dau_core.temporal import generate_market_stream
from dau_core.workflows import HeroWorkflow

stream = generate_market_stream(seed=7, num_symbols=3)
result = HeroWorkflow(bar_width=64, roll_window=3)(GenericContext(value=stream))
print(result.funnel)   # input -> matched -> bars -> rolling row counts
```

## Wire contracts

- `dau_core.registers`: the DAU magic word, register/protocol versions,
  register offsets, reset scopes, and the stream-job register contract
  (trigger/status/buffer addresses).
- `dau_core.stream`: Arrow-lite framing (`BatchHeader`,
  `ColumnDescriptor`, `OperationDescriptor`), logical types, operation
  codes, record-batch limits, and shared error codes.
- `dau_core.capabilities`: DMA constraints and device/operator
  capability models populated from a live device.
- `dau_core.capability_contract`: the composed capability advertisement
  a built shell publishes, derived from its composition spec.
- `dau_core.design`: the operator-unit inventory parsed back from a
  synthesized design's manifest.

## HDL and verification

`dau_core/hdl/` holds the SystemVerilog for every registered core, five of
them generated at render time. Tiles share a 64-bit valid/ready stream and
status contract: `input_last` ends a batch, a terminal tile closes out with
a status word and count, and mid-lane stages stay silent on success. The
tests run at three levels, all from `pytest`:

- text-contract checks over the packaged sources;
- standalone Verilator benches;
- cocotb-on-Verilator benches, golden-equal against the registered
  reference for each tile.

## Installation

Requires Python 3.11 or newer.

```bash
pip install dau-core
```

> [!NOTE]
> This library was generated using [copier](https://copier.readthedocs.io/en/stable/) from the [Base Python Project Template repository](https://github.com/python-project-templates/base).

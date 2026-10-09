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

The package also ships the SystemVerilog for every registered core as
importable resources. Every operator tile has a golden, a cocotb bench
under Verilator and measured resource points; the stream-infrastructure
blocks and shell adapters ship their HDL and are verified in the composed
shells rather than tile by tile. Five tiles' sources are generated from
their parameters at render time rather than checked in.

## Documentation

The documentation lives in [the wiki](https://github.com/dau-dev/dau-core/wiki),
authored under [`docs/wiki`](docs/wiki) and published from there. It is
organised by what you are trying to do.

- **Tutorial** — start here if the stack is new to you.
  [Read a tile's contract end to end](https://github.com/dau-dev/dau-core/wiki/Read-a-Tile-Contract)
  follows one operator through the five places its facts live.
- **How-to guides** — for a specific job.
  [Add an operator tile](https://github.com/dau-dev/dau-core/wiki/Add-an-Operator-Tile) is the
  golden-first sequence every tile in the palette went through;
  [test a core](https://github.com/dau-dev/dau-core/wiki/Test-a-Core) is the
  layered proof it has to survive; and
  [measure a resource envelope](https://github.com/dau-dev/dau-core/wiki/Measure-a-Resource-Envelope)
  is the one step that needs a synthesis host.
- **Reference** — the field-by-field description of
  [`CoreDefinition`](https://github.com/dau-dev/dau-core/wiki/Core-Definition), the registry's
  schema, and the
  [registry-wide guards](https://github.com/dau-dev/dau-core/wiki/Registry-Guards)
  with what each one refuses.
- **Explanation** — the reasoning behind the decisions that shape everything
  else:
  [why the registry is configuration](https://github.com/dau-dev/dau-core/wiki/Why-the-Registry-Is-Configuration),
  [why a golden model rather than a specification](https://github.com/dau-dev/dau-core/wiki/Why-Goldens-Define-Meaning),
  and [why resource envelopes are measurements that refuse](https://github.com/dau-dev/dau-core/wiki/Why-Resources-Are-Measured).

## The registry is configuration

There is no Python-literal list of cores. The registry is a
[Hydra](https://hydra.cc) config tree (`dau_core/config/dau-core/cores.yaml`,
one `_target_: dau_core.cores.CoreDefinition` entry per core) loaded into
the [ccflow](https://github.com/Point72/ccflow) `ModelRegistry` at
`/dau-core/<core-name>`. The package registers itself on the shared Hydra
search path via a `hydra.lernaplugins` entry point, so any composing
application discovers the cores without importing this package's code.

```python
from dau_core.registry import loaded_cores, core, sources_for

cores = loaded_cores()                     # name -> CoreDefinition, registry order
tile = core("int32-predicate-filter")      # one core
print(tile.kind.value)                     # "operator"
print(tile.golden)                         # "dau_core.operators:filter_values"
print(tile.resources)                      # measured LUT/FF/BRAM/DSP envelope

sources_for("stream-aggregation")    # dependency-closed HDL paths, in order
```

The registered palette today: 46 operator tiles (filters, maps,
aggregations both ordered and hash-mode, joins, membership, sorters,
partitioners, dispatchers, temporal operators, top-k, fixed-point
transcendentals, and the stateful group), 5 stream-infrastructure blocks
(AXI burst reader, broadcast, record writer, write mux, row unpacker),
4 shell adapters, 3 shared SV packages, and 1 simulation model: 59 entries
in all. `loaded_cores()` is the count that is never stale.
Accessors are read-through views: a registry an application has already
composed (including overrides) is the source of truth.

## Golden semantics

Each operator tile's behavior is defined by a pure function or small model
in `dau_core.operators` or `dau_core.temporal`.
Configuration objects are frozen [pydantic](https://docs.pydantic.dev)
models mirroring the tile's config ports, so the same object drives the
golden, the testbench, and design emission. Frozen here means more than
"no assignment": a mapping field is read-only, `model_copy` is backed by
`replace()` which validates the result, and `check()` re-validates a model
on demand, so a copy that skipped the validators is refused at the next
boundary rather than priced or hashed.

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

Pricing takes a coordinate. Every measurement-point core is looked up at
exactly that part and clock, and a coordinate nobody measured raises
`UnmeasuredResourceError` naming the gap rather than interpolating.

### Wide lanes

A quad-row operator core takes one row per beat at the rate its `ingest`
contract declares (one row per cycle or per two cycles, depending on the
tile). A `wide-row-stream` core takes the reader's WHOLE beat, `SLOTS = data_width / 128` quad rows every cycle with no dispatcher and no fan-out,
and the three row operators have wide
counterparts (`wide-row-predicate-filter`, `wide-row-map-alu`,
`wide-field-sum-aggregation`). The substitution is 1:1: same configs,
same order, only the core names change.

`data_width` must equal `SLOTS * 128` or construction raises
`CompositionError`. A wide lane cannot take the packed front unpacker —
packing trades DMA bytes for job time, and a wide lane wants whole rows.

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

The throughput this buys is a rate-model consequence, not a measurement
artifact: rows / SLOTS / clock. At SF1 that is 6,001,215 / 4 / 125 MHz =
12.0 ms, and silicon measures 12.4 ms.

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

`dau_core/hdl/` holds the SystemVerilog for every registered core (five
generated at render time). Tiles speak a common 64-bit valid/ready stream + status contract
(`input_last` frames a batch; a terminal tile closes out with a status
word and count, mid-lane stages are silent on success). Verification runs
three ways, all from `pytest`:

- text-contract checks over the packaged sources;
- standalone Verilator benches;
- cocotb-on-Verilator benches, golden-equal against the registered
  reference for each tile.

## Installation

Python 3.11 or newer.

```bash
pip install git+https://github.com/dau-dev/dau-core
```

The `dau-core` name on PyPI is a placeholder until the first release.

Development install and checks:

```bash
pip install -e .[develop]
python -m pytest dau_core/tests   # HDL benches need verilator on PATH
python -m ruff check dau_core
```

> [!NOTE]
> This library was generated using [copier](https://copier.readthedocs.io/en/stable/) from the [Base Python Project Template repository](https://github.com/python-project-templates/base).

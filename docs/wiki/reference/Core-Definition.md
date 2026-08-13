# `CoreDefinition` fields

Every entry in the core registry is a `CoreDefinition`. Entries are
declared in `dau_core/config/dau-core/cores.yaml`, one
`_target_: dau_core.cores.CoreDefinition` per HDL building block, and
loaded into the ccflow `ModelRegistry` at `/dau-core/<name>`.

## Identity

| Field            | Type                    | Description                                                                                                                                                                                  |
| ---------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name`           | `str`                   | Registry key. Operator tiles are conventionally `int32-<operation>`.                                                                                                                         |
| `kind`           | `CoreKind`              | `operator`, `stream-infra`, `shell-adapter`, `package`, or `sim-model`.                                                                                                                      |
| `module`         | `str`                   | The SystemVerilog module name.                                                                                                                                                               |
| `source`         | `str`                   | The HDL filename within `source_package`.                                                                                                                                                    |
| `source_package` | `str`                   | The importable package the `source` is resolved inside. Defaults to `dau_core.hdl`; a core registered from another distribution names its own package, so its HDL need not be vendored here. |
| `generator`      | `CoreGenerator \| None` | Set when the HDL is rendered rather than checked in. `source` then names the file it renders *to*, and `source_path()` raises `UnrenderedCoreError` until it exists.                         |
| `description`    | `str`                   | What the core does and what its contracts are.                                                                                                                                               |
| `depends`        | `tuple[str, ...]`       | Registry names this core's HDL requires. `sources_for()` resolves the closure in compile order.                                                                                              |

`CoreDefinition.render(into=)` writes a generated core's source;
`render_root()` is the default destination, overridable with the
`DAU_CORE_RENDER_ROOT` environment variable. Rendering a checked-in core is
refused.

## Semantics and verification

| Field    | Type          | Description                                                                                   |
| -------- | ------------- | --------------------------------------------------------------------------------------------- |
| `golden` | `str \| None` | The reference implementation, as `module:function`. Hardware results byte-compare against it. |
| `bench`  | `str \| None` | The cocotb bench module that proves the HDL equal to the golden.                              |

## Resources

| Field       | Type                                                          | Description                                                                             |
| ----------- | ------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| `resources` | `ResourceEnvelope \| tuple[ResourceMeasurement, ...] \| None` | Either one scalar envelope, or measurement points keyed by part, clock, and parameters. |

`ResourceEnvelope` carries `lut`, `ff`, `bram36`, `dsp`, `measured`.

`ResourceMeasurement` carries `part`, `clock_ns`, `params`, `lut`, `ff`,
`bram36`, `dsp`, `timing`, `wns_ns`, and exposes `usable_envelope()`.

`CoreDefinition.resource_measurement(part=, clock_ns=, params=)` returns the
point for an exact coordinate. It raises `UnmeasuredResourceError` when no
point matches and `TimingViolationError` when the matching point's `timing`
is `VIOLATED`. It does not interpolate or extrapolate.

## Stream contract

| Field                          | Type                   | Description                                                                                                                                                                     |
| ------------------------------ | ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `contract_kind`                | `ContractKind \| None` | `row-stream`, `wide-row-stream`, or the other declared framings.                                                                                                                |
| `record_words`                 | `int \| None`          | 64-bit words per input record.                                                                                                                                                  |
| `record_words_param`           | `str \| None`          | Module parameter that sets `record_words` when it is not fixed.                                                                                                                 |
| `output_record_words`          | `int \| None`          | Words per output record when it differs from the input.                                                                                                                         |
| `output_record_words_param`    | `str \| None`          | Module parameter that sets the output width.                                                                                                                                    |
| `output_record_words_variable` | `bool`                 | The output record's width depends on configuration (for example a field mask selecting how many sum words ride each record).                                                    |
| `count_port`                   | `str \| None`          | The port reporting how many records the core emitted.                                                                                                                           |
| `success_close_out`            | `bool`                 | Whether this core raises the batch's single success close-out. Terminal tiles do; mid-chain tiles are silent on success.                                                        |
| `supports_pipeline_depth`      | `bool`                 | The core accepts generic elastic register stages without changing framing semantics. Requires a declared `PIPELINE_DEPTH` parameter; no build or pricing consumer reads it yet. |

## Rate

| Field    | Type                 | Description                                                 |
| -------- | -------------------- | ----------------------------------------------------------- |
| `ingest` | `IngestRate \| None` | The declared acceptance rate. Required on every `operator`. |

An `IngestRate` carries `rows` per `per_cycles` and takes one of three
shapes. At most one scaling shape may be set.

| Shape    | Declaration          | Effective rate                        |
| -------- | -------------------- | ------------------------------------- |
| Plain    | `rows`, `per_cycles` | `rows / per_cycles`                   |
| Per slot | `scales_with_param`  | `rows / per_cycles × param`           |
| Framed   | `frames_on_param`    | `min(rows / per_cycles, param / 128)` |

`IngestRate.effective(params, declared)` resolves the rate against a
parameter set as an exact `Fraction`.

The declaration is **public interface, not a heuristic**: pricing and
scheduling rate-match a core they cannot inspect from it alone. It states
the *floor* a core sustains under continuous valid input, with record and
drain emission **amortized over the batch** — close-out records, top-k
drains, and per-group records are batch-tail or per-group costs, not
per-row costs.

Data-dependent worst cases — every group key distinct, adversarial key runs
into one dispatcher lane — are planner-regime concerns visible in the plan,
not floor violations. A core whose rate depends on data declares the
amortized cadence and names the adversarial case in its registry comment
rather than padding the number.

## Capability advertisement

| Field                | Type                        | Description                                             |
| -------------------- | --------------------------- | ------------------------------------------------------- |
| `opcodes`            | `tuple[OperationCode, ...]` | Operations this core executes in hardware.              |
| `opcode_input_types` | `tuple[LogicalType, ...]`   | Logical input types it accepts.                         |
| `unit_kind`          | `UnitKind \| None`          | `aggregation`, `sort`, `join`, `membership`, and so on. |
| `row_capacity`       | `int \| None`               | Bounded row capacity, for cores that hold a fixed set.  |

A composition built only from cores that declare none of these advertises
an empty opcode bitmap and is invisible to a planner matching capabilities.

## Storage and loading

| Field            | Type                        | Description                                                                                                                     |
| ---------------- | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `storage_intent` | `tuple[StorageIntent, ...]` | Declared on-chip or off-chip storage: `role`, `access` (`owned` or `shared`), `bytes_per_unit`, `scale_parameter`, `tier_hint`. |
| `load_image`     | `LoadImageKind \| None`     | The image format a two-phase LOAD accepts (`bitmap-64`, `quad-rows`).                                                           |

`CoreDefinition.storage_bytes(params)` prices the declared intents against
a parameter set, validating overrides against the declared parameter specs.

## Parameters

| Field        | Type                          | Description                                            |
| ------------ | ----------------------------- | ------------------------------------------------------ |
| `parameters` | `Mapping[str, ParameterSpec]` | Module parameters that may be overridden per instance. |

A `ParameterSpec` carries `default`, and optionally `minimum`, `maximum`,
`multiple_of`, `choices`, `power_of_two`, and `element_type`. These mirror
the RTL's elaboration guards, and `ParameterSpec.violation()` is the single
statement of the override rule — composition validation, the build layer,
and the pricing path all consult it rather than re-implementing the checks.

### Element types

`element_type: true` marks a parameter that names WHAT AN ELEMENT IS rather
than how big something is. Its `default` and `choices` may then be written
as type spellings, and a composition may override it the same way:

```yaml
FIELD_WIDTH:
  element_type: true
  default: int32
  choices: [int16, int32, int64]
  minimum: 8
  maximum: 64
```

The spellings are `intN`/`uintN`, `float32`/`float64`, and the fixed-point
form `qI.F` (I integer bits including the sign, F fractional, I+F total —
`q16.16` is 32 bits). They resolve to the encoding in
`dau_core/hdl/types.svh`, which tiles include to decode a type's width,
signedness, fractional bits and float-ness. A plain signed integer encodes
as its own width, so `int32` and `32` are the same declaration and an entry
migrated from a width names exactly the tile it always did.

Bounds on such a parameter constrain the type's WIDTH, because that is what
the RTL guard they mirror is about; `choices` match the whole type, so
`int32` and `q16.16` are not interchangeable despite both being 32 bits.

Bus and beat widths — `IN_WIDTH`, `OUT_WIDTH`, `DATA_WIDTH` — count bits per
handshake and are **not** element types. Leave `element_type` unset for them.

A tile that includes `types.svh` needs its directory on the include path;
`dau_core.hdl.DAU_HDL_INCLUDE_DIR` is that directory. Verilator does not
search the including file's own directory, so a bench or lint run that omits
`-I` fails on the include rather than on anything to do with the tile.

## Accessors

```python
from dau_core.cores import CoreKind
from dau_core.registry import loaded_cores, core, cores_of_kind, sources_for

loaded_cores()                       # name -> CoreDefinition, registry order
core("int32-streaming-top-k")        # one definition
cores_of_kind(CoreKind.OPERATOR)     # every operator tile
sources_for("int32-streaming-top-k") # dependency-closed HDL paths, in order
```

Accessors are read-through views over the composed registry: a registry an
application has already composed, including overrides, is the source of
truth.

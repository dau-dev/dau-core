# `CoreDefinition` fields

Every entry in the core registry is a `CoreDefinition`. Entries are
declared in `dau_core/config/dau-core/cores.yaml`, one
`_target_: dau_core.cores.CoreDefinition` per HDL building block, and
loaded into the ccflow `ModelRegistry` at `/dau-core/<name>`.

## Identity

| Field | Type | Description |
| --- | --- | --- |
| `name` | `str` | Registry key. Operator tiles are conventionally `int32-<operation>`. |
| `kind` | `CoreKind` | `operator`, `stream-infra`, `shell-adapter`, `package`, or `sim-model`. |
| `module` | `str` | The SystemVerilog module name. |
| `source` | `str` | The `.sv` filename within `dau_core/hdl/`. |
| `description` | `str` | What the core does and what its contracts are. |
| `depends` | `tuple[str, ...]` | Registry names this core's HDL requires. `sources_for()` resolves the closure in compile order. |

## Semantics and verification

| Field | Type | Description |
| --- | --- | --- |
| `golden` | `str \| None` | The reference implementation, as `module:function`. Hardware results byte-compare against it. |
| `bench` | `str \| None` | The cocotb bench module that proves the HDL equal to the golden. |

## Resources

| Field | Type | Description |
| --- | --- | --- |
| `resources` | `ResourceEnvelope \| tuple[ResourceMeasurement, ...] \| None` | Either one scalar envelope, or measurement points keyed by part, clock, and parameters. |

`ResourceEnvelope` carries `lut`, `ff`, `bram36`, `dsp`, `measured`.

`ResourceMeasurement` carries `part`, `clock_ns`, `params`, `lut`, `ff`,
`bram36`, `dsp`, `timing`, `wns_ns`, and exposes `usable_envelope()`.

`CoreDefinition.resource_measurement(part=, clock_ns=, params=)` returns the
point for an exact coordinate. It raises `UnmeasuredResourceError` when no
point matches and `TimingViolationError` when the matching point's `timing`
is `VIOLATED`. It does not interpolate or extrapolate.

## Stream contract

| Field | Type | Description |
| --- | --- | --- |
| `contract_kind` | `ContractKind \| None` | `row-stream`, `wide-row-stream`, or the other declared framings. |
| `record_words` | `int \| None` | 64-bit words per input record. |
| `record_words_param` | `str \| None` | Module parameter that sets `record_words` when it is not fixed. |
| `output_record_words` | `int \| None` | Words per output record when it differs from the input. |
| `output_record_words_param` | `str \| None` | Module parameter that sets the output width. |
| `output_record_words_variable` | `bool` | The output record's width depends on configuration (for example a field mask selecting how many sum words ride each record). |
| `count_port` | `str \| None` | The port reporting how many records the core emitted. |
| `success_close_out` | `bool` | Whether this core raises the batch's single success close-out. Terminal tiles do; mid-chain tiles are silent on success. |
| `supports_pipeline_depth` | `bool` | Declared; currently unconsumed. |

## Rate

| Field | Type | Description |
| --- | --- | --- |
| `ingest` | `IngestRate \| None` | The declared acceptance rate: `rows` per `per_cycles`, or a shape that scales with a parameter. |

An `IngestRate` is a declaration under **stated assumptions**, not a
guarantee. A core whose rate depends on data (probe depth, same-lane runs)
declares the amortized cadence and names the adversarial case in its
registry comment rather than padding the number.

## Capability advertisement

| Field | Type | Description |
| --- | --- | --- |
| `opcodes` | `tuple[OperationCode, ...]` | Operations this core executes in hardware. |
| `opcode_input_types` | `tuple[LogicalType, ...]` | Logical input types it accepts. |
| `unit_kind` | `UnitKind \| None` | `aggregation`, `sort`, `join`, `membership`, and so on. |
| `row_capacity` | `int \| None` | Bounded row capacity, for cores that hold a fixed set. |

A composition built only from cores that declare none of these advertises
an empty opcode bitmap and is invisible to a planner matching capabilities.

## Storage and loading

| Field | Type | Description |
| --- | --- | --- |
| `storage_intent` | `tuple[StorageIntent, ...]` | Declared on-chip or off-chip storage: `role`, `access` (`owned` or `shared`), `bytes_per_unit`, `scale_parameter`, `tier_hint`. |
| `load_image` | `LoadImageKind \| None` | The image format a two-phase LOAD accepts (`bitmap-64`, `quad-rows`). |

`CoreDefinition.storage_bytes(params)` prices the declared intents against
a parameter set, validating overrides against the declared parameter specs.

## Parameters

| Field | Type | Description |
| --- | --- | --- |
| `parameters` | `Mapping[str, ParameterSpec]` | Module parameters that may be overridden per instance. |

A `ParameterSpec` carries `default`, and optionally `minimum`, `maximum`,
`multiple_of`, `choices`, and `power_of_two`. These mirror the RTL's
elaboration guards, and `ParameterSpec.violation()` is the single statement
of the override rule — composition validation, the build layer, and the
pricing path all consult it rather than re-implementing the checks.

## Accessors

```python
from dau_core.cores import loaded_cores, core, cores_of_kind, sources_for, CoreKind

loaded_cores()                       # name -> CoreDefinition, registry order
core("int32-streaming-top-k")        # one definition
cores_of_kind(CoreKind.OPERATOR)     # every operator tile
sources_for("int32-streaming-top-k") # dependency-closed HDL paths, in order
```

Accessors are read-through views over the composed registry: a registry an
application has already composed, including overrides, is the source of
truth.

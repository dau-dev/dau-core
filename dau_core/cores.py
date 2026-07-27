"""Core registry models and accessors: every HDL building block, what it
is, what it depends on, which golden defines its semantics, and what it
costs.

The registry itself lives in dau-core's hydra config tree
(``dau_core/config/dau-core/cores.yaml``): one ``CoreDefinition`` entry per
core, composed into the ccflow ``ModelRegistry`` at ``/dau-core/<name>``
and discoverable on the shared search path via the ``hydra.lernaplugins``
entry point. Composition IS the registry — this module defines the models
and thin cached accessors, never a core list. Resource envelopes are
produced by the dau-build synthesize-cores task; ``measured=False`` marks
estimates awaiting a synthesis pass.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from importlib import import_module
from pathlib import Path

from ccflow import BaseModel
from pydantic import ConfigDict

from .design import UnitKind
from .stream import LogicalType, OperationCode


class CoreKind(Enum):
    OPERATOR = "operator"
    STREAM_INFRA = "stream-infra"
    SHELL_ADAPTER = "shell-adapter"
    PACKAGE = "package"
    SIM_MODEL = "sim-model"


class LoadImageKind(Enum):
    """What a load-capable tile's LOAD-phase input stream is (the two-phase
    load: cfg_load=1 streams the build image, cfg_load=0 probes). The
    composition validates image/length-grid rules per kind; a core without
    this marker has no cfg_load port and can never be phase-bound."""

    BITMAP_64 = "bitmap-64"  # KEY_SPACE/64 little-endian 64-bit words (membership bitmap)
    QUAD_ROWS = "quad-rows"  # 16-byte build rows (hash-join key + payload)


class ContractKind(Enum):
    """Which streaming interface contract an operator tile speaks. Contract
    tests select their validator from this tag; a new core declares its
    contract here rather than being special-cased in test code."""

    ROW_STREAM = "row-stream"  # the standard 64-bit stream + status tile contract
    ARROW_LITE_DESCRIPTOR = "arrow-lite-descriptor"  # descriptor-driven Arrow-lite protocol


class ResourceEnvelope(BaseModel):
    """Post-synthesis footprint on the target part (dpv1 XC7A200T unless
    noted). ``measured=False`` marks an engineering estimate."""

    model_config = ConfigDict(frozen=True)

    lut: int
    ff: int
    bram36: float
    dsp: int
    measured: bool = True


class ParameterSpec(BaseModel):
    """An overridable SystemVerilog module parameter: its HDL default and the
    constraint a composition override must satisfy — mirroring the RTL's own
    elaboration guards so an invalid override is rejected at composition time,
    never later as an HDL elaboration failure. ``multiple_of`` mirrors sizing
    (the membership bitmap is ``KEY_SPACE/64`` words); ``choices`` pins an
    exact legal set (the quad-stream widths are 64 or 128, nothing else);
    ``power_of_two`` mirrors mask/bit-select decodes (the key-mask dispatcher's
    ``NUM_PARTITIONS``); ``minimum`` mirrors a lower bound (the rolling-moment
    window is ``>= 2`` — a size-1 sample variance is undefined). The HDL is the
    source of truth; this mirrors it."""

    model_config = ConfigDict(frozen=True)

    default: int
    multiple_of: int = 1
    choices: tuple[int, ...] | None = None
    power_of_two: bool = False
    minimum: int = 1
    maximum: int | None = None


class CoreDefinition(BaseModel):
    """One HDL building block and its contract surround."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: CoreKind
    module: str
    source: str
    description: str
    depends: tuple[str, ...] = ()
    golden: str | None = None  # "module:callable" defining the semantics
    bench: str | None = None  # cocotb bench module
    resources: ResourceEnvelope | None = None
    count_port: str | None = None  # the tile's trailing status counter, if any
    legacy: bool = False  # predates the stream+status contract; kept verified, not for new compositions
    contract_kind: ContractKind | None = None  # required for non-legacy operators; None elsewhere (no declared stream contract)
    # capability facts (HDL truth, consumed by the composition-derived
    # capability contract): the descriptor-selectable OperationCodes a tile
    # executes in hardware and the logical types those opcodes accept.
    # AVERAGE is never declared here — it is not a hardware opcode (the host
    # divides the SUM/COUNT results at protocol level).
    opcodes: tuple[OperationCode, ...] = ()
    opcode_input_types: tuple[LogicalType, ...] = ()
    row_capacity: int | None = None  # batch row capacity for buffered tiles (bitonic sorter CAPACITY)
    unit_kind: UnitKind | None = None  # the design-inventory unit an instance of this tile contributes
    # HDL status truth: True when the tile emits a status close-out after
    # every successfully completed batch (terminal-tile behavior — the record
    # writer's done waits on it); False when the tile is silent on success
    # and raises status only for what the downstream cannot see (zero-rows
    # compaction, torn row, bad config — the chain-stage/partition contract,
    # exactly one close-out per lane per batch).
    success_close_out: bool = False
    # the two-phase load capability: what this tile's LOAD-phase input stream
    # is (None = no cfg_load port; the tile can never be phase-bound)
    load_image: LoadImageKind | None = None
    # overridable SystemVerilog module parameters and their HDL defaults (the
    # membership filter's KEY_SPACE, the broadcast hash join's KEY_SPACE, ...).
    # A composition may override a declared parameter to build a non-default
    # core size (the module-parameter channel); an override of an undeclared
    # name is rejected. The values here mirror the .sv parameter defaults —
    # the HDL is the source of truth, this is the machine-readable declaration
    # the composition validates against.
    parameters: Mapping[str, ParameterSpec] = {}

    def source_path(self) -> Path:
        from . import hdl

        return Path(str(hdl.__path__[0])) / self.source

    def golden_callable(self):
        if self.golden is None:
            return None
        module_name, _, attr = self.golden.partition(":")
        return getattr(import_module(module_name), attr)


class UnknownCoreError(KeyError):
    pass


def loaded_cores() -> Mapping[str, CoreDefinition]:
    """The core registry, name -> definition in registry order: dau-core's
    hydra config tree (``dau-core/cores.yaml``) composed into the ccflow
    ``ModelRegistry`` at ``/dau-core/<core-name>``. Composition IS the
    registry — there is no Python-literal core list; these accessors are
    thin read-through views for in-process consumers (tests, dau shell
    walking). A ``/dau-core`` subregistry a consumer already composed
    (possibly with overrides) is the source of truth and is never reloaded
    or overwritten here; the packaged defaults load only when it is absent."""
    from ccflow import ModelRegistry

    subregistry = ModelRegistry.root().models.get("dau-core")
    if subregistry is None:
        from dau_core.config import load_config

        subregistry = load_config().models["dau-core"]
    return {name: model for name, model in subregistry.models.items() if isinstance(model, CoreDefinition)}


def core(name: str) -> CoreDefinition:
    try:
        return loaded_cores()[name]
    except KeyError:
        raise UnknownCoreError(f"unknown core {name!r}; known: {sorted(loaded_cores())}") from None


def core_for_module(module: str) -> CoreDefinition:
    for definition in loaded_cores().values():
        if definition.module == module:
            return definition
    raise UnknownCoreError(f"no core defines module {module!r}")


def cores_of_kind(kind: CoreKind) -> tuple[CoreDefinition, ...]:
    return tuple(definition for definition in loaded_cores().values() if definition.kind == kind)


def selectable_operators() -> tuple[CoreDefinition, ...]:
    """Operator tiles a planner or new composition may pick — legacy cores
    (predating the stream+status contract) are excluded. Legacy tiles stay
    in the registry, verified against their goldens, but are not offered
    for new work."""
    return tuple(definition for definition in cores_of_kind(CoreKind.OPERATOR) if not definition.legacy)


def sources_for(*names: str) -> tuple[Path, ...]:
    """Dependency-closed, deduplicated source paths for a set of cores —
    packages and dependencies first, in registry order."""
    selected: dict[str, CoreDefinition] = {}

    def visit(name: str) -> None:
        definition = core(name)
        for dependency in definition.depends:
            visit(dependency)
        selected.setdefault(definition.name, definition)

    for name in names:
        visit(name)
    ordered = [definition for definition in loaded_cores().values() if definition.name in selected]
    return tuple(definition.source_path() for definition in ordered)

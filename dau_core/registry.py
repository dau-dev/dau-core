"""Read-through access to THIS package's registry of tiles.

Composition IS the registry: the definitions live in dau-core's hydra
config tree (``dau-core/cores.yaml``) composed into the ccflow
``ModelRegistry`` at ``/dau-core/<core-name>``, and there is no
Python-literal core list anywhere. These accessors are thin views for
in-process consumers -- tests, shell walking, source staging.

They are deliberately NOT part of the tile authoring surface. A third
party declaring a tile needs the models in :mod:`dau_core.cores`; it must
never need this module, which knows only about the tiles dau-core itself
ships. Keeping the two apart is what lets the surface be reachable by an
author who does not have this repository -- see
``tests/test_public_tile_surface.py``, which enforces the direction.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .cores import CoreDefinition, CoreKind


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

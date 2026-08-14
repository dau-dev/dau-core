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


class DuplicateCoreError(ValueError):
    """Two composed groups declare the same core name."""


def loaded_cores() -> Mapping[str, CoreDefinition]:
    """Every composed core, name -> definition, dau-core's own first.

    dau-core's tiles come from its hydra config tree
    (``dau-core/cores.yaml``) composed into the ccflow ``ModelRegistry`` at
    ``/dau-core/<core-name>``. Composition IS the registry — there is no
    Python-literal core list; these accessors are thin read-through views
    for in-process consumers (tests, dau shell walking). A ``/dau-core``
    subregistry a consumer already composed (possibly with overrides) is the
    source of truth and is never reloaded or overwritten here; the packaged
    defaults load only when it is absent.

    **A CONTRIBUTED core is a core.** A third party composing a
    ``CoreDefinition`` under its own group used to compose successfully,
    register, and then be invisible to every consumer, because this only
    ever read ``/dau-core``: the author did everything right and their tile
    silently vanished, with no error to explain it. Any ``CoreDefinition``
    the application composed is returned, whichever group holds it.

    This does not blur the packages. ``source_package`` is the seam that
    tells a foreign tile from one of ours, and dau-core's own guards already
    scope themselves by it rather than assuming every registered core is
    theirs. A name collision is raised rather than resolved, because
    silently preferring one definition is the failure this fixes.
    """
    from ccflow import ModelRegistry

    root = ModelRegistry.root()
    subregistry = root.models.get("dau-core")
    if subregistry is None:
        from dau_core.config import load_config

        subregistry = load_config().models["dau-core"]
    cores = {name: model for name, model in subregistry.models.items() if isinstance(model, CoreDefinition)}

    for group_name, group in root.models.items():
        if group_name == "dau-core" or not hasattr(group, "models"):
            continue
        for name, model in group.models.items():
            if not isinstance(model, CoreDefinition):
                continue
            existing = cores.get(name)
            if existing is not None and existing is not model:
                raise DuplicateCoreError(
                    f"core {name!r} is declared by more than one composed group: "
                    f"{existing.source_package!r} and {model.source_package!r} "
                    f"(the second was found under {group_name!r}). Rename one; a core name is how "
                    "every design, spec hash and cached manifest refers to it, so resolving this "
                    "silently would let a design mean two different things."
                )
            cores[name] = model
    return cores


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

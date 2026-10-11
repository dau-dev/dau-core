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
from typing import TYPE_CHECKING

from .cores import CoreDefinition, CoreKind, CoreRole

if TYPE_CHECKING:
    from ccflow import ModelRegistry


class UnknownCoreError(KeyError):
    pass


class DuplicateCoreError(ValueError):
    """Two composed groups declare the same core name."""


def _groups() -> tuple[tuple[str, ModelRegistry], ...]:
    """The composed registry groups, dau-core's own first.

    Composing the packaged defaults when ``/dau-core`` is absent is what
    makes every accessor here work from a bare interpreter; a ``/dau-core``
    a consumer already composed (possibly with overrides) is never reloaded.
    """
    from ccflow import ModelRegistry

    root = ModelRegistry.root()
    if root.models.get("dau-core") is None:
        from dau_core.config import load_config

        load_config()
    own = root.models.get("dau-core")
    groups: list[tuple[str, ModelRegistry]] = [("dau-core", own)] if own is not None else []
    groups.extend((name, group) for name, group in root.models.items() if name != "dau-core" and hasattr(group, "models"))
    return tuple(groups)


def _entry(group: ModelRegistry, name: str) -> CoreDefinition | None:
    """``name`` in ``group``, building only that one model.

    A keyed lookup is the whole point: the groups are ccflow
    ``LazyRegistry`` instances, so this materializes one ``CoreDefinition``
    where reading the group's items would materialize all of them.
    """
    try:
        model = group[name]
    except KeyError:
        return None
    return model if isinstance(model, CoreDefinition) else None


def _declared_names(group: ModelRegistry) -> tuple[str, ...]:
    """Every entry name in ``group``, without building any of its models."""
    return tuple(group.models)


def core(name: str) -> CoreDefinition:
    """The composed core called ``name``.

    A keyed lookup per group, so asking for one tile builds one model. The
    duplicate check still runs: a name declared by two composed groups is
    refused here exactly as :func:`loaded_cores` refuses it, because a core
    name is how every design, spec hash and cached manifest refers to it.
    """
    found: CoreDefinition | None = None
    found_in = ""
    for group_name, group in _groups():
        model = _entry(group, name)
        if model is None or model is found:
            continue
        if found is not None:
            raise DuplicateCoreError(
                f"core {name!r} is declared by more than one composed group: "
                f"{found.source_package!r} (under {found_in!r}) and {model.source_package!r} "
                f"(under {group_name!r}). Rename one; a core name is how every design, spec hash "
                "and cached manifest refers to it, so resolving this silently would let a design "
                "mean two different things."
            )
        found, found_in = model, group_name
    if found is None:
        known = sorted({entry for _name, group in _groups() for entry in _declared_names(group)})
        raise UnknownCoreError(f"unknown core {name!r}; known: {known}")
    return found


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
    cores: dict[str, CoreDefinition] = {}
    for group_name, group in _groups():
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


def core_for_module(module: str) -> CoreDefinition:
    for definition in loaded_cores().values():
        if definition.module == module:
            return definition
    raise UnknownCoreError(f"no core defines module {module!r}")


def cores_of_kind(kind: CoreKind) -> tuple[CoreDefinition, ...]:
    return tuple(definition for definition in loaded_cores().values() if definition.kind == kind)


def cores_of_role(role: CoreRole) -> tuple[CoreDefinition, ...]:
    """Every composed core declaring ``role``, in registry order."""
    return tuple(definition for definition in loaded_cores().values() if definition.role is role)


def core_of_role(role: CoreRole) -> CoreDefinition:
    """The one composed core declaring ``role``.

    The shell-infrastructure roles are jobs exactly one core fills, and a
    composition selects the core by asking for the job. Zero is a palette
    with a hole in it; two is an ambiguity nothing here can resolve, and
    silently taking the first would make the answer depend on registry
    order, so both refuse.
    """
    found = cores_of_role(role)
    if len(found) != 1:
        names = [definition.name for definition in found]
        raise UnknownCoreError(f"expected exactly one composed core with role {role.value!r}, found {len(found)}: {names}")
    return found[0]


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

"""The modules a third-party tile author needs, and what they may import.

A tile contributed from outside this package declares a ``CoreDefinition``,
speaks the row-stream ABI, names the element types its parameters take, and
may render its HDL through a ``CoreGenerator``. Those modules are
therefore the *authoring surface* -- the part that has to be reachable by
someone who will never have this repository, whether that eventually means
extracting them into a public distribution or publishing dau-core itself.

That only stays true if the surface keeps its dependencies shallow. This
guard enumerates what it may import. It is not a style rule: every name
added here is a module that would have to be carried along, and a surface
that quietly grows a dependency on the composition logic, the goldens, or
the workload code stops being extractable without anyone noticing.

The guard reads imports statically rather than importing the modules,
because an import-time check would pass on a dependency that is only
reached inside a function.
"""

from __future__ import annotations

import ast
from pathlib import Path

import dau_core

# The authoring surface: models + the stream ABI + the element type
# vocabulary + the generator protocol. element_types is here rather than
# recorded as debt because a tile declared elsewhere has to spell its types
# the SAME way this one does -- "int32" and "q16.16" mean a particular
# encoding, which the author's own HDL decodes out of the shipped
# dau_core/hdl/types.svh. A private copy of that vocabulary would be a
# second encoding, which is the failure this whole mechanism exists to
# prevent.
# `models` is here because it IS the declaration language: a CoreDefinition
# is a CheckedModel and a CoreGenerator carries CheckedMixin, so an author
# declaring either already has it. It depends on nothing but ccflow and
# pydantic, which the surface may use anyway.
SURFACE_MODULES = ("cores", "design", "element_types", "generators", "models", "stream")

# Third-party runtime dependencies the surface may use. ccflow and pydantic
# ARE the declaration language -- a CoreDefinition is a ccflow BaseModel, so
# an author already depends on them.
ALLOWED_THIRD_PARTY = {"ccflow", "pydantic"}

# Modules inside dau-core the surface may reach for. Each entry is a
# commitment: it travels with the surface.
ALLOWED_INTERNAL = {
    *SURFACE_MODULES,
    # DAU_MAGIC_WORD only. The magic word is protocol, not implementation --
    # an author encoding a status record has to write it -- but it lives in
    # the register map alongside a great deal that is NOT the tile surface.
    # Extracting the surface means taking the constant with it, not
    # registers.py entire.
    "registers",
}

# Dependencies the surface has TODAY that an extraction has to resolve.
# Recorded rather than hidden, and pinned so it cannot grow: each entry is a
# task, and the set being empty is what "the surface is extractable"
# actually means.
#
# It is empty. It held `config` until the registry accessors moved to
# dau_core.registry: cores.py had been two things in one file -- the
# declaration models a third-party author needs, and the accessors that
# load THIS package's own tiles, which reached for the packaged config to
# do it. An author needs the former and must never carry the latter.
EXTRACTION_DEBT: set[str] = set()


def _surface_paths() -> tuple[Path, ...]:
    root = Path(dau_core.__path__[0])
    return tuple(root / f"{name}.py" for name in SURFACE_MODULES)


def _internal_imports(path: Path) -> set[str]:
    """Top-level dau-core modules this file imports, relative or absolute."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # `from .stream import ...`
                if node.module:
                    found.add(node.module.split(".")[0])
            elif node.module and node.module.split(".")[0] == "dau_core":
                parts = node.module.split(".")
                if len(parts) > 1:
                    found.add(parts[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "dau_core" and len(parts) > 1:
                    found.add(parts[1])
    return found


def test_the_authoring_surface_stays_extractable() -> None:
    for path in _surface_paths():
        assert path.is_file(), f"{path.name} is named as authoring surface but does not exist"
        leaked = _internal_imports(path) - ALLOWED_INTERNAL - EXTRACTION_DEBT
        assert not leaked, (
            f"{path.name} imports {sorted(leaked)} from dau-core's private side. The tile authoring surface "
            f"({', '.join(SURFACE_MODULES)}) has to be reachable by an author who does not have this repository; "
            "every module it reaches for has to travel with it. Either move what is needed into the surface, or "
            "keep the dependency out of it"
        )


def test_the_surface_declares_its_third_party_dependencies() -> None:
    """The authoring surface may not grow a heavy runtime dependency.

    An author installs this to declare a tile, not to run a build: pulling
    in hydra, a simulator, or a synthesis toolchain here would make the
    declaration surface as expensive as the toolchain it feeds.
    """
    for path in _surface_paths():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            root = None
            if isinstance(node, ast.ImportFrom) and not node.level and node.module:
                root = node.module.split(".")[0]
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    candidate = alias.name.split(".")[0]
                    if candidate not in _STDLIB and candidate != "dau_core":
                        assert candidate in ALLOWED_THIRD_PARTY, f"{path.name} imports {candidate!r}"
                continue
            if root and root not in _STDLIB and root != "dau_core":
                assert root in ALLOWED_THIRD_PARTY, (
                    f"{path.name} imports {root!r}, which is not part of the tile declaration language "
                    f"({sorted(ALLOWED_THIRD_PARTY)}). An author installs this to DECLARE a tile, not to build one"
                )


_STDLIB = {
    "__future__",
    "abc",
    "ast",
    "collections",
    "dataclasses",
    "enum",
    "fractions",
    "functools",
    "hashlib",
    "importlib",
    "itertools",
    "math",
    "os",
    "pathlib",
    "re",
    "struct",
    "typing",
}


def test_the_extraction_debt_does_not_grow() -> None:
    """The recorded debt is a ceiling, not a budget.

    A new private dependency in the authoring surface is cheap to add and
    expensive to discover later -- it is only visible at the moment someone
    tries to ship the surface without this repository. Pinning the set means
    that moment happens now, in a test, rather than then.
    """
    observed: set[str] = set()
    for path in _surface_paths():
        observed |= _internal_imports(path) - ALLOWED_INTERNAL
    assert observed <= EXTRACTION_DEBT, (
        f"the tile authoring surface grew a new private dependency: {sorted(observed - EXTRACTION_DEBT)}. "
        "Keep it out, or record it as extraction debt with the reason"
    )
    # and the recorded debt is real, not stale
    assert observed == EXTRACTION_DEBT, (
        f"extraction debt {sorted(EXTRACTION_DEBT - observed)} is recorded but no longer present -- delete it, so the set keeps meaning what it says"
    )

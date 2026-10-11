"""Importing this package must stay cheap, and its exports must stay honest.

Every model here is a ccflow model, and importing ccflow imports csp and
pandas: about 0.9 s. Hydra's searchpath plugin imports ``dau_core.config``
to discover what is installed, so a package that imports its model modules
at import time puts that 0.9 s in front of every composition, for names
discovery never reads.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import dau_core

INIT = Path(dau_core.__file__)


def _init_tree() -> ast.Module:
    return ast.parse(INIT.read_text(encoding="utf-8"))


def _type_checking_imports() -> dict[str, tuple[str, ...]]:
    """What the ``TYPE_CHECKING`` block declares, read from the source."""
    for node in ast.walk(_init_tree()):
        if isinstance(node, ast.If) and getattr(node.test, "id", "") == "TYPE_CHECKING":
            return {
                statement.module: tuple(alias.name for alias in statement.names)
                for statement in node.body
                if isinstance(statement, ast.ImportFrom) and statement.module
            }
    raise AssertionError("the package __init__ has no TYPE_CHECKING block")


def test_the_declaration_and_the_type_checking_block_agree() -> None:
    """The two exist for different readers -- the resolver and a type
    checker -- and a name in one and not the other is a name that imports
    for one of them and fails for the other."""
    assert _type_checking_imports() == {module: tuple(names) for module, names in dau_core._EXPORTS.items()}


def test_every_exported_name_resolves() -> None:
    """A name declared here and missing from its submodule would raise on
    first access, which is a broken import nobody sees until a caller
    reaches for it. Resolve all of them now."""
    for name in dau_core.__all__:
        assert getattr(dau_core, name) is not None or name in dau_core.__all__
    assert sorted(dau_core.__all__) == list(dau_core.__all__)
    assert "__version__" not in dau_core.__all__


def test_an_unknown_name_still_raises_attribute_error() -> None:
    with pytest.raises(AttributeError, match="has no attribute 'not_a_real_export'"):
        dau_core.not_a_real_export  # noqa: B018


def test_importing_the_package_does_not_import_ccflow() -> None:
    """The guard that keeps composition fast. Run in a fresh interpreter,
    because this one has already imported everything."""
    probe = "import sys; import dau_core, dau_core.config; print(int('ccflow' in sys.modules), int('pandas' in sys.modules))"
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    ccflow_loaded, pandas_loaded = result.stdout.split()
    assert ccflow_loaded == "0", "importing dau_core pulled in ccflow; an export or dau_core.config imports it at module level"
    assert pandas_loaded == "0", "importing dau_core pulled in pandas"


def test_a_name_reached_through_the_package_is_the_submodule_object() -> None:
    """Lazily resolved is not a copy: the name a caller imports from the
    package is the one the submodule defines, so an isinstance check against
    either spelling holds."""
    from dau_core.registers import RegisterOffset

    assert dau_core.RegisterOffset is RegisterOffset

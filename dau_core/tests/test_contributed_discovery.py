"""A contributing package's tiles compose without anyone naming them.

Two things had to be true for contribution to work and neither was:

* the tiles must ARRIVE. ``base.yaml`` selects one option from the group
  ``dau-core``, so a package shipping ``dau-core/<theirs>.yaml`` adds an
  option that is never composed — no error, no warning. Hydra refuses the
  additive form for a single-select group, and ``override`` would replace
  our palette with theirs.
* the tiles must be FOUND once composed, which is :mod:`dau_core.registry`'s
  half (see ``test_contributed_cores.py``).

This covers the first. Contributions live in their own group, and its
options are selected together AS A LIST — the form that admits more than
one. A bare ``+group=option`` selects exactly one, so two installed vendors
would silently mean one vendor, which is the same class of failure as
vanishing entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ccflow import ModelRegistry

from dau_core.config import CONTRIB_GROUP, compose_config, contributed_options

_TILE = """\
{name}:
  _target_: dau_core.cores.CoreDefinition
  name: {name}
  kind: operator
  module: {module}
  source: {module}.sv
  source_package: {package}.hdl
  description: a contributed tile
  contract_kind: row-stream
  record_words: 1
  ingest:
    _target_: dau_core.cores.IngestRate
    rows: 1
    per_cycles: 1
"""


@pytest.fixture
def contrib_dir(tmp_path):
    """A searchpath directory holding contributed tile options."""

    def write(*names: str) -> str:
        group = tmp_path / CONTRIB_GROUP
        group.mkdir(exist_ok=True)
        for name in names:
            module = name.replace("-", "_")
            (group / f"{name}.yaml").write_text(_TILE.format(name=f"{name}-tile", module=module, package=module))
        return str(tmp_path)

    return write


def test_options_are_discovered_not_named(contrib_dir) -> None:
    path = contrib_dir("vendor-a", "vendor-b")
    assert contributed_options(path) == ("vendor-a", "vendor-b")


def test_no_contributions_installed_is_silent(tmp_path) -> None:
    """The common case must cost nothing and say nothing."""
    assert contributed_options(str(tmp_path)) == ()
    assert contributed_options() == ()


def test_every_contributor_composes_not_just_the_first(contrib_dir) -> None:
    """The list form is the whole point: selecting one option per group would
    make a second installed vendor silently disappear."""
    path = contrib_dir("vendor-a", "vendor-b")
    composed = compose_config(config_dir=path).cfg.get(CONTRIB_GROUP)
    assert set(composed) == {"vendor-a-tile", "vendor-b-tile"}


def test_our_own_palette_survives_contribution(contrib_dir) -> None:
    path = contrib_dir("vendor-a")
    composed = compose_config(config_dir=path).cfg
    assert len(composed["dau-core"]) > 50, "contribution must add to the palette, never replace it"


def test_an_explicit_selection_is_not_overridden(contrib_dir) -> None:
    """A caller who named the contributions it wants means it."""
    path = contrib_dir("vendor-a", "vendor-b")
    composed = compose_config([f"+{CONTRIB_GROUP}=vendor-a"], config_dir=path).cfg.get(CONTRIB_GROUP)
    assert set(composed) == {"vendor-a-tile"}


def test_contributed_tiles_reach_the_registry(contrib_dir) -> None:
    """The two halves together: composed AND findable, with the seam intact."""
    from dau_core.config import load_config
    from dau_core.registry import core, loaded_cores

    path = contrib_dir("vendor-a")
    try:
        load_config(config_dir=path, overwrite=True)
        assert core("vendor-a-tile").source_package == "vendor_a.hdl"
        own = [d for d in loaded_cores().values() if d.source_package == "dau_core.hdl"]
        assert len(own) > 50 and all(d.name != "vendor-a-tile" for d in own)
    finally:
        ModelRegistry.root().clear()
        load_config(overwrite=True)


def test_discovery_inside_an_existing_initialization_leaves_it_standing(contrib_dir) -> None:
    """The regression that broke two downstream suites.

    Hydra's global state is process-wide. A first version of discovery
    initialized its own instance and cleared it in a `finally`, which tore
    down whatever the CALLER was inside -- dau-sim and dau-polars then
    asserted `is_initialized()` against nothing. Discovery must reuse an
    initialization it did not create, and must leave it exactly as found.
    """
    from hydra import initialize_config_dir
    from hydra.core.global_hydra import GlobalHydra

    import dau_core.config
    from dau_core.config import contributed_options

    path = contrib_dir("vendor-a")
    parent = str(Path(dau_core.config.__file__).parent)

    GlobalHydra.instance().clear()
    with initialize_config_dir(config_dir=parent, version_base=None):
        assert GlobalHydra.instance().is_initialized()
        contributed_options(path)
        assert GlobalHydra.instance().is_initialized(), "discovery tore down a caller's hydra"
    GlobalHydra.instance().clear()


def test_discovery_never_registers_anything(contrib_dir) -> None:
    """Initializing hydra runs the searchpath plugin, which LOADS provider
    configs into the root registry. Asking what is installed must not change
    what is installed, or the composition that follows fails adding `dau-core`
    a second time."""
    from dau_core.config import contributed_options

    path = contrib_dir("vendor-a")
    before = set(ModelRegistry.root().models)
    assert contributed_options(path) == ("vendor-a",)
    assert set(ModelRegistry.root().models) == before


def test_an_unreadable_searchpath_does_not_take_down_composition(tmp_path) -> None:
    """Discovery is best-effort. A directory hydra cannot read must cost the
    contributions, not the compose that would otherwise have worked."""
    from dau_core.config import compose_config, contributed_options

    missing = str(tmp_path / "does-not-exist")
    assert contributed_options(missing) == ()
    assert len(compose_config().cfg["dau-core"]) > 50

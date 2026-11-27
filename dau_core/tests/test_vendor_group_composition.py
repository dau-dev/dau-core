"""Another package's cores are an ordinary config group, composed when it
is selected.

A package ships ``<group>/cores.yaml`` on the hydra search path. The group
cannot be ``dau-core``: ``base.yaml`` already selects from it, and hydra
takes one selection per group. Being on the search path makes a group
available; an override or a defaults list selects it, and only then are
its cores composed at ``/<group>/<core-name>``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from ccflow import ModelRegistry

from dau_core.config import compose_config, load_config

_CORE = """\
{name}:
  _target_: dau_core.cores.CoreDefinition
  name: {name}
  kind: operator
  module: {module}
  source: {module}.sv
  source_package: {package}.hdl
  description: a core another package declares
  contract_kind: row-stream
  record_words: 1
  ingest:
    _target_: dau_core.cores.IngestRate
    rows: 1
    per_cycles: 1
"""


@pytest.fixture
def search_path(tmp_path):
    """A search path directory holding one ``<group>/cores.yaml`` per name."""

    def write(*groups: str) -> str:
        for group in groups:
            (tmp_path / group).mkdir(exist_ok=True)
            module = group.replace("-", "_")
            (tmp_path / group / "cores.yaml").write_text(_CORE.format(name=f"{group}-core", module=module, package=module))
        return str(tmp_path)

    return write


@pytest.fixture
def restored_registry():
    """Put the packaged composition back after a test loads another one."""
    yield
    ModelRegistry.root().clear()
    load_config(overwrite=True)


def test_a_group_on_the_search_path_is_not_composed_until_selected(search_path) -> None:
    path = search_path("vendor-a", "vendor-b")

    assert set(compose_config(config_dir=path).cfg) == {"dau-core", "dau-tiles"}
    one = compose_config(["+vendor-a=cores"], config_dir=path).cfg
    assert set(one) == {"dau-core", "dau-tiles", "vendor-a"} and set(one["vendor-a"]) == {"vendor-a-core"}
    both = compose_config(["+vendor-a=cores", "+vendor-b=cores"], config_dir=path).cfg
    assert set(both["vendor-a"]) == {"vendor-a-core"} and set(both["vendor-b"]) == {"vendor-b-core"}
    assert len(both["dau-core"]) > 50, "another package's cores add to the palette, never replace it"


def test_selecting_an_option_that_is_not_there_is_hydras_error(search_path) -> None:
    from hydra.errors import MissingConfigException

    path = search_path("vendor-a")
    with pytest.raises(MissingConfigException, match="Could not find 'vendor-a/absent'"):
        compose_config(["+vendor-a=absent"], config_dir=path)


def test_a_second_option_cannot_join_the_dau_core_group(tmp_path) -> None:
    """Why another package needs a group of its own."""
    from hydra.errors import ConfigCompositionException

    (tmp_path / "dau-core").mkdir()
    (tmp_path / "dau-core" / "vendor.yaml").write_text(_CORE.format(name="vendor-core", module="vendor", package="vendor"))
    with pytest.raises(ConfigCompositionException, match="Multiple values for dau-core"):
        compose_config(["+dau-core=vendor"], config_dir=str(tmp_path))


def test_a_selected_groups_cores_reach_the_registry_with_their_group(search_path, restored_registry) -> None:
    from dau_core.registry import core, core_group, loaded_cores

    path = search_path("vendor-a", "vendor-b")
    load_config(["+vendor-a=cores"], config_dir=path, overwrite=True)

    assert core("vendor-a-core").source_package == "vendor_a.hdl"
    assert core_group("vendor-a-core") == "vendor-a"
    assert core_group("row-map-alu") == "dau-core"
    assert "vendor-b-core" not in loaded_cores(), "on the search path, never selected"
    own = [definition for definition in loaded_cores().values() if definition.source_package == "dau_core.hdl"]
    assert len(own) > 50 and all(definition.name != "vendor-a-core" for definition in own)


def test_a_group_the_application_composed_is_kept(search_path, restored_registry) -> None:
    """A CLI may compose another package's group from its own search path
    before anything asks dau-core for a core. Loading dau-core's palette
    afterwards must keep that group, not fail adding a second one."""
    from omegaconf import OmegaConf

    from dau_core.registry import core

    root = ModelRegistry.root()
    root.clear()
    root.load_config(OmegaConf.create({"vendor-a": OmegaConf.load(Path(search_path("vendor-a")) / "vendor-a" / "cores.yaml")}))

    load_config()
    assert core("vendor-a-core").source_package == "vendor_a.hdl"
    assert core("row-map-alu").source_package == "dau_core.hdl"

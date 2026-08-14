"""A core someone else declared is still a core.

A third party composing a ``CoreDefinition`` under its own registry group
used to compose successfully, register, and then be invisible: every
consumer reaches tiles through :func:`loaded_cores`, which read ``/dau-core``
and nothing else. The author did everything the authoring surface asks for
and their tile silently vanished, with no error naming the cause -- the
worst shape a failure can take, because there is nothing to search for.

The seam that keeps packages apart is ``source_package``, not invisibility.
dau-core's own guards already scope themselves by it rather than assuming
every registered core is theirs, so admitting foreign cores here does not
loosen a single one of them.
"""

from __future__ import annotations

import pytest
from ccflow import ModelRegistry
from omegaconf import OmegaConf

from dau_core.registry import DuplicateCoreError, UnknownCoreError, core, loaded_cores

_VENDOR_TILE = {
    "_target_": "dau_core.cores.CoreDefinition",
    "kind": "operator",
    "module": "vendor_widget",
    "source": "vendor_widget.sv",
    "source_package": "vendor_tiles.hdl",
    "description": "a tile declared outside dau-core",
    "contract_kind": "row-stream",
    "record_words": 1,
    "ingest": {"_target_": "dau_core.cores.IngestRate", "rows": 1, "per_cycles": 1},
}


@pytest.fixture
def registry_group():
    """Compose a group, then drop it so the registry is not left mutated."""
    added: list[str] = []

    def compose(group: str, name: str, **overrides):
        ModelRegistry.root().load_config(OmegaConf.create({group: {name: {**_VENDOR_TILE, "name": name, **overrides}}}))
        added.append(group)

    yield compose
    root = ModelRegistry.root()
    for group in added:
        root.remove(group)


def test_a_contributed_core_is_visible_to_consumers(registry_group) -> None:
    before = len(loaded_cores())
    registry_group("vendor-tiles", "vendor-widget")
    assert len(loaded_cores()) == before + 1
    assert core("vendor-widget").source_package == "vendor_tiles.hdl"


def test_contribution_does_not_disturb_our_own_cores(registry_group) -> None:
    """The seam still works: ours remain identifiable, and a foreign tile
    does not become one of ours by being visible."""
    own_before = {name for name, d in loaded_cores().items() if d.source_package == "dau_core.hdl"}
    registry_group("vendor-tiles", "vendor-widget")
    own_after = {name for name, d in loaded_cores().items() if d.source_package == "dau_core.hdl"}
    assert own_after == own_before
    assert "vendor-widget" not in own_after


def test_a_name_collision_is_raised_not_resolved(registry_group) -> None:
    """A core name is how every design, spec hash and cached manifest refers
    to a tile. Silently preferring one definition would let a design mean two
    different things depending on what else was installed."""
    registry_group("vendor-tiles", "int32-map-alu")
    with pytest.raises(DuplicateCoreError, match="declared by more than one composed group"):
        loaded_cores()


def test_an_absent_core_still_reports_unknown() -> None:
    with pytest.raises(UnknownCoreError):
        core("no-such-core-anywhere")

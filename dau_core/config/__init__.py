"""dau-core's hydra config tree — the core registry.

Hydra composition + the ccflow ``ModelRegistry`` ARE the registry: every
HDL building block is a ``CoreDefinition`` entry in ``dau-core/cores.yaml``,
loaded at registry path ``/dau-core/<core-name>``. This package is exposed
on the shared hydra searchpath via the ``hydra.lernaplugins`` entry point,
so dau-build / dau-sim compose it without importing dau-core code.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from ccflow import ModelRegistry
from ccflow.utils.hydra import ConfigLoadResult, load_config as base_load_config

__all__ = ("compose_config", "load_config")


def compose_config(
    overrides: Sequence[str] | None = None,
    *,
    config_dir: str | None = None,
    version_base: str | None = None,
) -> ConfigLoadResult:
    """Compose dau-core's own config tree (group ``dau-core``, option
    ``cores``) with raw overrides."""
    parent_dir = str(Path(__file__).resolve().parent)
    return base_load_config(
        root_config_dir=parent_dir,
        root_config_name="base",
        config_dir=config_dir,
        overrides=list(overrides or ()),
        version_base=version_base,
        basepath=parent_dir,
        debug=False,
    )


def load_config(
    overrides: Sequence[str] | None = None,
    *,
    overwrite: bool = False,
    config_dir: str | None = None,
    version_base: str | None = None,
) -> ModelRegistry:
    """Compose and load the core registry into the root ``ModelRegistry``."""
    result = compose_config(overrides, config_dir=config_dir, version_base=version_base)
    registry = ModelRegistry.root()
    registry.load_config(result.cfg, overwrite=overwrite)
    return registry

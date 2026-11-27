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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ccflow import ModelRegistry
    from ccflow.utils.hydra import ConfigLoadResult

# ccflow is imported inside the functions below, not here. Hydra's searchpath
# plugin imports this package to discover what is installed, and importing
# ccflow pulls in its whole stack (~0.9 s) for names discovery never reads.

__all__ = ("compose_config", "load_config")


def compose_config(
    overrides: Sequence[str] | None = None,
    *,
    config_dir: str | None = None,
    version_base: str | None = None,
) -> ConfigLoadResult:
    """Compose dau-core's own config tree (group ``dau-core``, option
    ``cores``) with raw overrides.

    Another package's cores are an ordinary config group on the search
    path, ``<group>/cores.yaml``, composed at ``/<group>/<core-name>``.
    Nothing here looks for them: they are composed when an override
    (``+<group>=cores``) or a defaults list selects them, as any hydra
    option is. ``dau-core`` cannot be that group, because ``base.yaml``
    already selects from it and hydra takes one selection per group.
    """
    from ccflow.utils.hydra import load_config as base_load_config

    parent_dir = str(Path(__file__).resolve().parent)
    return base_load_config(
        root_config_dir=parent_dir,
        root_config_name="base",
        config_dir=config_dir or "",  # ccflow treats an empty directory like an absent one
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
    """Compose and load the core registry into the root ``ModelRegistry``.

    The composed groups are loaded as ccflow ``LazyRegistry`` children, so a
    ``CoreDefinition`` is built when something asks for that core rather than
    all 59 at load. Loading the palette eagerly cost 164 ms and every
    consumer paid it to reach one core; lazily it is 10 ms plus about 4 ms
    per core actually used (:func:`dau_core.registry.core`). A consumer that
    does need the whole palette -- the guards, source staging -- asks for it
    through :func:`dau_core.registry.loaded_cores` and pays for it there.
    """
    from ccflow import LazyRegistry, ModelRegistry

    result = compose_config(overrides, config_dir=config_dir, version_base=version_base)
    registry = ModelRegistry.root()
    composed = LazyRegistry()
    composed.load_config(cfg=result.cfg, overwrite=True)
    for name, group in composed.models.items():
        if not overwrite and name in registry.models:
            # the application composed this group itself (a CLI whose design
            # or overrides selected another package's cores): its
            # composition is the source of truth, as for /dau-core
            continue
        registry.add(name, group, overwrite=overwrite)
    return registry

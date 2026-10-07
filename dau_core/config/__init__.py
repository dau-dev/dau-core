"""dau-core's hydra config tree — the core registry.

Hydra composition + the ccflow ``ModelRegistry`` ARE the registry: every
HDL building block is a ``CoreDefinition`` entry in ``dau-core/cores.yaml``,
loaded at registry path ``/dau-core/<core-name>``. This package is exposed
on the shared hydra searchpath via the ``hydra.lernaplugins`` entry point,
so dau-build / dau-sim compose it without importing dau-core code.
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from pathlib import Path

from ccflow import ModelRegistry
from ccflow.utils.hydra import ConfigLoadResult, load_config as base_load_config

__all__ = ("CONTRIB_GROUP", "compose_config", "contributed_options", "load_config")

# Where a contributing package puts its tiles. `dau-core` itself cannot be
# that place: `base.yaml` selects ONE option from it, so a second package
# shipping `dau-core/<theirs>.yaml` adds an option that is never composed --
# no error, no warning, the tile simply is not there. Hydra actively refuses
# the additive form for a single-select group, and `override` would REPLACE
# our palette with theirs.
CONTRIB_GROUP = "dau-core-contrib"


def contributed_options(config_dir: str | None = None, *, version_base: str | None = None) -> tuple[str, ...]:
    """Contributed tile options installed on the searchpath, discovered.

    A consumer should not have to name the vendors it happens to have
    installed -- that is the difference between a package being installable
    and a package being usable. Hydra can enumerate a group's options across
    the whole searchpath, so ask it.
    """
    from hydra import initialize_config_dir
    from hydra.core.global_hydra import GlobalHydra

    parent_dir = str(Path(__file__).resolve().parent)
    searchpath = [f"file://{config_dir}"] if config_dir else []
    overrides = [f"hydra.searchpath=[{','.join(searchpath)}]"] if searchpath else []
    instance = GlobalHydra.instance()

    def options(loader) -> tuple[str, ...]:
        return tuple(sorted(loader.get_group_options(CONTRIB_GROUP, overrides=overrides)))

    # NEVER tear down an initialization we did not create. Hydra's global
    # state is shared with every caller in the process, and clearing it under
    # one leaves it asserting `is_initialized()` against nothing -- which is
    # exactly what a first attempt here did to two downstream suites.
    if instance.is_initialized():
        try:
            return options(instance.config_loader())
        except Exception as error:  # noqa: BLE001  # discovery is best-effort; never fail a compose over it
            warnings.warn(f"contributed tile discovery failed; composing dau-core's own palette only: {error!r}", stacklevel=2)
            return ()

    # Initializing hydra runs the searchpath plugin, which LOADS provider
    # configs into the root registry as a side effect -- discovery would
    # otherwise register `dau-core` and the composition that follows would
    # then fail adding it a second time. Asking what is installed must not
    # change what is installed, so undo anything discovery registered.
    root = ModelRegistry.root()
    before = set(root.models)
    try:
        with initialize_config_dir(config_dir=parent_dir, version_base=version_base):
            return options(instance.config_loader())
    except Exception as error:  # noqa: BLE001
        # discovery is best-effort: a searchpath hydra cannot read must not
        # take down composition that would otherwise have worked, but a
        # contributor whose tiles vanish deserves to be told why
        warnings.warn(f"contributed tile discovery failed; composing dau-core's own palette only: {error!r}", stacklevel=2)
        return ()
    finally:
        for added in set(root.models) - before:
            root.remove(added)
        # we initialized it, so we own tearing it down -- but only then
        if instance.is_initialized():
            instance.clear()


def compose_config(
    overrides: Sequence[str] | None = None,
    *,
    config_dir: str | None = None,
    version_base: str | None = None,
) -> ConfigLoadResult:
    """Compose dau-core's own config tree (group ``dau-core``, option
    ``cores``) with raw overrides.

    Contributed tiles are composed too, without the caller naming them: the
    options of :data:`CONTRIB_GROUP` are discovered across the searchpath and
    selected together as a list, which is the form that admits more than one
    (a bare ``+group=option`` selects exactly one, so two installed vendors
    would silently mean one). A contributor therefore never edits
    ``cores.yaml``, which is the whole point of contribution.

    Two-phase -- initialize to discover, then compose -- because a group's
    options cannot be listed without a searchpath, and the searchpath is what
    the composition is about to be built from.
    """
    parent_dir = str(Path(__file__).resolve().parent)
    resolved = list(overrides or ())
    if not any(override.lstrip("+~").startswith(f"{CONTRIB_GROUP}=") for override in resolved):
        contributed = contributed_options(config_dir, version_base=version_base)
        if contributed:
            resolved.append(f"+{CONTRIB_GROUP}=[{','.join(contributed)}]")
    return base_load_config(
        root_config_dir=parent_dir,
        root_config_name="base",
        config_dir=config_dir or "",  # ccflow treats an empty directory like an absent one
        overrides=resolved,
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

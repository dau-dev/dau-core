"""A tile declares the polars expression it computes.

A compiler reaches a contributed tile through this declaration, so it is
checked where the tile is declared: a lowering names a quad-row tile that
rewrites one field in place, and that tile's golden is the element function
the compiler runs on the host to answer the same query.
"""

from __future__ import annotations

import pytest
from ccflow import ModelRegistry
from omegaconf import OmegaConf

from dau_core.cores import CoreDefinition, ExpressionLowering
from dau_core.registry import core

_CLIP = {
    "function": "Clip",
    "options": {"has_min": True, "has_max": True},
    "source_port": "cfg_src_field",
    "destination_port": "cfg_dst_field",
    "scalar_ports": ["cfg_lower", "cfg_upper"],
    "scale_invariant": True,
}

_TILE = {
    "name": "vendor-clamp",
    "kind": "operator",
    "module": "vendor_clamp",
    "source": "vendor_clamp.sv",
    "source_package": "vendor_tiles.hdl",
    "description": "clamps one field of each quad row",
    "golden": "builtins:min",
    "contract_kind": "row-stream",
    "record_words": 2,
    "ingest": {"rows": 1, "per_cycles": 2},
}


def test_a_composed_tile_carries_its_lowering() -> None:
    entry = {**_TILE, "_target_": "dau_core.cores.CoreDefinition", "lowering": {**_CLIP, "_target_": "dau_core.cores.ExpressionLowering"}}
    root = ModelRegistry.root()
    root.load_config(OmegaConf.create({"vendor-tiles": {"vendor-clamp": entry}}))
    try:
        lowering = core("vendor-clamp").lowering
        assert lowering is not None
        assert lowering.function == "Clip"
        assert dict(lowering.options) == {"has_min": True, "has_max": True}
        assert lowering.scalar_ports == ("cfg_lower", "cfg_upper")
    finally:
        root.remove("vendor-tiles")


def test_a_lowering_needs_a_quad_row_tile() -> None:
    with pytest.raises(ValueError, match="row-stream tile of 2 words"):
        CoreDefinition.model_validate({**_TILE, "record_words": 1, "lowering": _CLIP})


def test_a_lowering_needs_a_golden() -> None:
    with pytest.raises(ValueError, match="no golden element function"):
        CoreDefinition.model_validate({**_TILE, "golden": None, "lowering": _CLIP})


def test_a_lowering_binds_each_port_once() -> None:
    with pytest.raises(ValueError, match="binds a config port twice"):
        ExpressionLowering.model_validate({**_CLIP, "scalar_ports": ["cfg_lower", "cfg_src_field"]})

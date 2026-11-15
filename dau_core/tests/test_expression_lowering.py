"""A tile declares the expression it computes, as a pattern over a fixed op
vocabulary, so a compiler can reach it.

The declaration is checked where the tile is declared: ops are known and
take their own number of args, every placeholder is bound to one port, and
the tile is a quad-row tile with a golden element function.
"""

from __future__ import annotations

import pytest
from ccflow import ModelRegistry
from omegaconf import OmegaConf

from dau_core.cores import CoreDefinition, ExpressionLowering, ExpressionOp, ExpressionPattern
from dau_core.registry import core, lowered_cores

_CLIP = {
    "pattern": {"op": "clip", "args": [{"column": "value"}, {"literal": "lower"}, {"literal": "upper"}]},
    "bind": {"value": "cfg_src_field", "lower": "cfg_lower", "upper": "cfg_upper"},
    "destination_port": "cfg_dst_field",
    "scale_invariant": True,
}

# clip(a * (1 - b), lower, upper): one tile for a whole q1-shaped expression
_DISCOUNTED_CLIP = {
    "pattern": {
        "op": "clip",
        "args": [
            {"op": "mul", "args": [{"column": "price"}, {"op": "sub", "args": [{"literal": 1}, {"column": "discount"}]}]},
            {"literal": "lower"},
            {"literal": "upper"},
        ],
    },
    "bind": {"price": "cfg_price_field", "discount": "cfg_discount_field", "lower": "cfg_lower", "upper": "cfg_upper"},
    "destination_port": "cfg_dst_field",
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


def test_a_composed_tile_carries_its_lowering_and_is_found() -> None:
    entry = {**_TILE, "_target_": "dau_core.cores.CoreDefinition", "lowering": {**_CLIP, "_target_": "dau_core.cores.ExpressionLowering"}}
    root = ModelRegistry.root()
    before = lowered_cores()
    root.load_config(OmegaConf.create({"vendor-tiles": {"vendor-clamp": entry}}))
    try:
        lowering = core("vendor-clamp").lowering
        assert lowering is not None
        assert lowering.pattern.op is ExpressionOp.CLIP
        assert lowering.pattern.placeholders() == ("value", "lower", "upper")
        assert [definition.name for definition in lowered_cores()] == [*(definition.name for definition in before), "vendor-clamp"]
    finally:
        root.remove("vendor-tiles")


def test_a_compound_pattern_names_its_columns_and_constants() -> None:
    lowering = ExpressionLowering.model_validate(_DISCOUNTED_CLIP)
    assert lowering.pattern.columns() == ("price", "discount")
    assert lowering.pattern.placeholders() == ("price", "discount", "lower", "upper")
    assert lowering.pattern.size() == 8
    constant = lowering.pattern.args[0].args[1].args[0]
    assert constant.literal == 1 and constant.placeholders() == ()


@pytest.mark.parametrize(
    ("pattern", "message"),
    [
        ({"op": "clamp", "args": [{"column": "x"}]}, "clamp"),
        ({"op": "clip", "args": [{"column": "x"}, {"literal": "lo"}]}, "clip takes 3 args"),
        ({"column": "x", "literal": "y"}, "exactly one of op, column or literal"),
        ({"column": "x", "args": [{"column": "y"}]}, "takes no args"),
    ],
)
def test_a_malformed_pattern_is_refused(pattern: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ExpressionPattern.model_validate(pattern)


def test_every_placeholder_is_bound_once() -> None:
    with pytest.raises(ValueError, match="bind must name every placeholder"):
        ExpressionLowering.model_validate({**_CLIP, "bind": {"value": "cfg_src_field", "lower": "cfg_lower"}})
    with pytest.raises(ValueError, match="config port is bound twice"):
        ExpressionLowering.model_validate({**_CLIP, "bind": {"value": "cfg_src_field", "lower": "cfg_lower", "upper": "cfg_lower"}})
    twice = {"op": "add", "args": [{"column": "x"}, {"column": "x"}]}
    with pytest.raises(ValueError, match="placeholder appears twice"):
        ExpressionLowering.model_validate({"pattern": twice, "bind": {"x": "cfg_a"}, "destination_port": "cfg_dst"})
    with pytest.raises(ValueError, match="at least one column"):
        ExpressionLowering.model_validate({"pattern": {"literal": "c"}, "bind": {"c": "cfg_c"}, "destination_port": "cfg_dst"})


def test_a_lowering_needs_a_quad_row_tile_with_a_golden() -> None:
    with pytest.raises(ValueError, match="row-stream tile of 2 words"):
        CoreDefinition.model_validate({**_TILE, "record_words": 1, "lowering": _CLIP})
    with pytest.raises(ValueError, match="no golden element function"):
        CoreDefinition.model_validate({**_TILE, "golden": None, "lowering": _CLIP})

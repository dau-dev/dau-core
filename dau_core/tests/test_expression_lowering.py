"""A tile declares the polars expression it computes, so a compiler can
reach it.

The expression's columns are placeholders; ``bind`` wires each one to a
config port, and ``literals`` says which stand for query literals. The
declaration is checked where the tile is declared, and the tile must be a
quad-row tile with a golden element function.
"""

from __future__ import annotations

import polars as pl
import pytest
from ccflow import ModelRegistry
from omegaconf import OmegaConf

from dau_core.cores import CoreDefinition, ExpressionLowering
from dau_core.registry import core, lowered_cores

_CLIP = {
    "expression": 'pl.col("value").clip(pl.col("lower"), pl.col("upper"))',
    "literals": ["lower", "upper"],
    "bind": {"value": "cfg_src_field", "lower": "cfg_lower", "upper": "cfg_upper"},
    "destination_port": "cfg_dst_field",
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


def test_a_composed_tile_carries_its_lowering_and_is_found() -> None:
    entry = {**_TILE, "_target_": "dau_core.cores.CoreDefinition", "lowering": {**_CLIP, "_target_": "dau_core.cores.ExpressionLowering"}}
    root = ModelRegistry.root()
    before = lowered_cores()
    root.load_config(OmegaConf.create({"vendor-tiles": {"vendor-clamp": entry}}))
    try:
        lowering = core("vendor-clamp").lowering
        assert lowering is not None
        assert lowering.placeholders() == ("value", "lower", "upper")
        assert lowering.columns() == ("value",)
        assert lowering.expression_json()["Function"]["function"] == {"Clip": {"has_min": True, "has_max": True}}
        assert [definition.name for definition in lowered_cores()] == [*(definition.name for definition in before), "vendor-clamp"]
    finally:
        root.remove("vendor-tiles")


def test_text_and_a_polars_expression_declare_the_same_pattern() -> None:
    from_text = ExpressionLowering.model_validate(_CLIP)
    from_expr = ExpressionLowering.model_validate({**_CLIP, "expression": pl.col("value").clip(pl.col("lower"), pl.col("upper"))})
    assert isinstance(from_expr.expression, dict)
    assert from_text.expression_json() == from_expr.expression_json()
    # the declaration serializes either way, which is what a capture cache keys on
    assert from_expr.model_dump_json()


def test_a_compound_expression_names_its_columns_and_constants() -> None:
    lowering = ExpressionLowering.model_validate(
        {
            "expression": '(pl.col("price") * (1 - pl.col("discount"))).clip(pl.col("lower"), pl.col("upper"))',
            "literals": ["lower", "upper"],
            "bind": {"price": "cfg_price_field", "discount": "cfg_discount_field", "lower": "cfg_lower", "upper": "cfg_upper"},
            "destination_port": "cfg_dst_field",
        }
    )
    assert lowering.columns() == ("price", "discount")
    assert lowering.placeholders() == ("price", "discount", "lower", "upper")
    assert lowering.size() > ExpressionLowering.model_validate(_CLIP).size()


def test_a_malformed_declaration_is_refused() -> None:
    with pytest.raises(ValueError, match="not a polars expression"):
        ExpressionLowering.model_validate({**_CLIP, "expression": 'pl.col("value").clamp(1, 2)'})
    with pytest.raises(ValueError, match="bind must name every placeholder"):
        ExpressionLowering.model_validate({**_CLIP, "bind": {"value": "cfg_src_field", "lower": "cfg_lower"}})
    with pytest.raises(ValueError, match="config port is bound twice"):
        ExpressionLowering.model_validate({**_CLIP, "bind": {"value": "cfg_src_field", "lower": "cfg_lower", "upper": "cfg_lower"}})
    with pytest.raises(ValueError, match="are not placeholders"):
        ExpressionLowering.model_validate({**_CLIP, "literals": ["lower", "upper", "nope"]})
    with pytest.raises(ValueError, match="at least one column"):
        ExpressionLowering.model_validate({**_CLIP, "literals": ["value", "lower", "upper"]})


def test_a_lowering_needs_a_quad_row_tile_with_a_golden() -> None:
    with pytest.raises(ValueError, match="row-stream tile of 2 words"):
        CoreDefinition.model_validate({**_TILE, "record_words": 1, "lowering": _CLIP})
    with pytest.raises(ValueError, match="no golden element function"):
        CoreDefinition.model_validate({**_TILE, "golden": None, "lowering": _CLIP})


_DISTINCT = {
    "expression": 'pl.col("value").n_unique()',
    "bind": {"value": {"field": "cfg_value_field", "shift": "cfg_value_shift", "width": "VALUE_BITS"}},
    "group": {"shift": "cfg_group_shift", "width": "GROUP_BITS"},
}


def test_an_aggregation_declares_a_lane_terminal_reading_packed_columns() -> None:
    lowering = ExpressionLowering.model_validate(_DISTINCT)
    assert lowering.is_aggregate()
    assert lowering.columns() == ("value",)
    value = lowering.binding("value")
    assert (value.field, value.shift, value.width) == ("cfg_value_field", "cfg_value_shift", "VALUE_BITS")
    # a plain field binding reads as one too
    assert ExpressionLowering.model_validate(_CLIP).binding("value").field == "cfg_src_field"


def test_a_terminal_and_a_row_stage_are_not_confused() -> None:
    with pytest.raises(ValueError, match="aggregation is a lane terminal"):
        ExpressionLowering.model_validate({**_DISTINCT, "group": None})
    with pytest.raises(ValueError, match="aggregation is a lane terminal"):
        ExpressionLowering.model_validate({**_DISTINCT, "destination_port": "cfg_dst_field"})
    with pytest.raises(ValueError, match="row expression writes destination_port"):
        ExpressionLowering.model_validate({**_CLIP, "group": {"shift": "cfg_group_shift"}})
    with pytest.raises(ValueError, match="names no field port"):
        ExpressionLowering.model_validate({**_DISTINCT, "bind": {"value": {"shift": "cfg_value_shift"}}})
    with pytest.raises(ValueError, match="binds to one value port"):
        ExpressionLowering.model_validate({**_CLIP, "bind": {"value": "cfg_src_field", "lower": {"field": "cfg_lower"}, "upper": "cfg_upper"}})
    with pytest.raises(ValueError, match="config port is bound twice"):
        ExpressionLowering.model_validate({**_DISTINCT, "group": {"shift": "cfg_value_shift"}})


def test_an_aggregating_tile_may_terminate_a_wide_lane() -> None:
    wide = {**_TILE, "contract_kind": "wide-row-stream", "lowering": _DISTINCT}
    assert CoreDefinition.model_validate(wide).lowering is not None
    with pytest.raises(ValueError, match="row-stream tile of 2 words"):
        CoreDefinition.model_validate({**wide, "lowering": _CLIP})

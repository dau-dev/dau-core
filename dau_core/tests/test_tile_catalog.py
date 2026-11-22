"""The tile catalog states which polars expressions the device answers and
which cores compute each one.

The model checks a tile's own wiring where it is declared. These tests check
the composed catalog against the registry and the HDL: a tile names cores
that exist, on config ports and parameters their modules declare.
"""

from __future__ import annotations

import re

import pytest
from ccflow import ModelRegistry
from omegaconf import OmegaConf

from dau_core.composition import LOAD_PHASE_BINDING
from dau_core.cores import ContractKind
from dau_core.registry import DuplicateTileError, UnknownTileError, contributed_tiles, core, loaded_tiles, sources_for, tile
from dau_core.tiles import TileDefinition

_COMPARE = {
    "name": "compare",
    "kind": "filter",
    "description": "keeps rows where a column is under a constant",
    "literals": ["bound"],
    "expressions": [{"expression": 'pl.col("value") < pl.col("bound")', "config": {"cfg_op0": "LT"}}],
    "cores": [{"core": "row-predicate-filter", "inputs": {"value": "cfg_field0", "bound": "cfg_constant0"}}],
}


def _declared(**changes: object) -> dict[str, object]:
    return {**_COMPARE, **changes}


def test_a_tile_resolves_each_expression_to_per_core_config() -> None:
    declared = TileDefinition.model_validate(_COMPARE)
    (expression,) = declared.expressions
    assert declared.placeholders(expression) == ("value", "bound")
    assert declared.config_for(expression) == {"row-predicate-filter": {"cfg_op0": "LT"}}
    assert declared.cores[0].taken("value").ports == ("cfg_field0",)


def test_a_frame_shape_names_its_columns_and_arguments() -> None:
    declared = TileDefinition.model_validate(
        {
            "name": "top-k",
            "kind": "order",
            "description": "the k rows with the largest key",
            "arguments": ["k"],
            "expressions": [{"frame": 'frame.sort("key", descending=True).head(k)'}],
            "cores": [{"core": "streaming-top-k", "inputs": {"key": {"source": "row-key"}, "k": {"parameter": "K"}}}],
            "result": "k rows",
        }
    )
    assert declared.placeholders(declared.expressions[0]) == ("key", "k")
    assert declared.cores[0].parameters() == ("K",)
    grouped = TileDefinition.model_validate(
        {
            "name": "group-sum",
            "kind": "grouped-aggregate",
            "description": "per-group sums",
            "expressions": [{"frame": 'frame.group_by("group").agg(pl.col("value").sum())'}],
            "cores": [{"core": "wide-group-table-sum", "inputs": {"value": {"mask": "cfg_field_mask"}, "group": {"shift": "cfg_group_shift"}}}],
            "result": "one record per group",
        }
    )
    assert grouped.placeholders(grouped.expressions[0]) == ("group", "value")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"cores": [{"core": "row-predicate-filter", "inputs": {"value": "cfg_field0"}}]}, "reach no core"),
        (
            {"cores": [{"core": "row-predicate-filter", "inputs": {"value": "cfg_field0", "bound": "cfg_constant0", "other": "cfg_field1"}}]},
            "neither placeholders",
        ),
        ({"literals": ["limit"]}, "not placeholders"),
        ({"expressions": [{"expression": 'pl.col("value") < pl.col("bound")', "config": {"cfg_field0": 1}}]}, "cannot also hold a fixed value"),
        ({"expressions": [{"expression": 'pl.col("value") < pl.col("bound")', "config": {"other-core.cfg_op0": "LT"}}]}, "names no core"),
        ({"expressions": [{"expression": "pl.col("}]}, "not polars source"),
        ({"expressions": [{"expression": 'pl.col("value") < pl.col("bound")', "frame": "frame"}]}, "one of the two"),
        ({"arguments": ["window"]}, "not placeholders"),
        ({"arguments": ["bound"]}, "both a literal and an argument"),
        ({"kind": "map"}, "needs an output"),
        ({"kind": "aggregate"}, "what the lane returns"),
        (
            {
                "cores": [
                    {"core": "row-predicate-filter", "inputs": {"value": {"ports": ["cfg_field0"], "mask": "cfg_mask"}, "bound": "cfg_constant0"}}
                ]
            },
            "one way",
        ),
    ],
)
def test_a_miswired_tile_is_refused(changes: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        TileDefinition.model_validate(_declared(**changes))


def test_a_packed_column_names_its_field_offset_and_width() -> None:
    declared = TileDefinition.model_validate(
        {
            "name": "distinct-count",
            "kind": "grouped-aggregate",
            "description": "distinct values per group",
            "expressions": [{"frame": 'frame.group_by("group").agg(pl.col("value").n_unique())'}],
            "cores": [
                {
                    "core": "vendor-distinct-count",
                    "inputs": {
                        "value": {"ports": ["cfg_value_field"], "shift": "cfg_value_shift", "width": "VALUE_BITS"},
                        "group": {"shift": "cfg_group_shift", "width": "GROUP_BITS"},
                    },
                }
            ],
            "result": "one record per group",
        }
    )
    value = declared.cores[0].taken("value")
    assert value.config_ports() == ("cfg_value_field", "cfg_value_shift")
    assert declared.cores[0].parameters() == ("VALUE_BITS", "GROUP_BITS")
    assert declared.scale_invariant is False


def test_a_contributed_tile_is_found_apart_from_the_catalog() -> None:
    """A tile composed under another package's group is a tile, and a
    compiler can ask for the ones dau-core did not declare."""
    entry = {
        **_COMPARE,
        "_target_": "dau_core.tiles.TileDefinition",
        "name": "vendor-clamp",
        "kind": "map",
        "literals": ["lower", "upper"],
        "expressions": [{"expression": 'pl.col("value").clip(pl.col("lower"), pl.col("upper"))'}],
        "cores": [
            {
                "core": "vendor-clamp",
                "inputs": {"value": "cfg_src_field", "lower": "cfg_lower", "upper": "cfg_upper"},
                "output": {"name": "clamped", "ports": ["cfg_dst_field"]},
            }
        ],
        "scale_invariant": True,
    }
    root = ModelRegistry.root()
    assert contributed_tiles() == {}
    root.load_config(OmegaConf.create({"vendor-tiles": {"vendor-clamp": entry}}))
    try:
        assert list(contributed_tiles()) == ["vendor-clamp"]
        assert contributed_tiles()["vendor-clamp"].scale_invariant
        assert "vendor-clamp" in loaded_tiles() and "compare" in loaded_tiles()
    finally:
        root.remove("vendor-tiles")
    root.load_config(OmegaConf.create({"vendor-tiles": {"compare": {**entry, "name": "compare"}}}))
    try:
        with pytest.raises(DuplicateTileError, match="compare"):
            loaded_tiles()
        with pytest.raises(DuplicateTileError, match="vendor-tiles"):
            tile("compare")
    finally:
        root.remove("vendor-tiles")


def test_a_tile_validates_where_polars_is_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading a shape's placeholders needs polars. Without it the
    declaration still loads, and a compiler, which has polars, checks the
    wiring."""
    from dau_core.tiles import TileExpression

    def unavailable(self: TileExpression, arguments: tuple[str, ...] = ()) -> dict:
        raise ImportError("polars")

    monkeypatch.setattr(TileExpression, "plan_json", unavailable)
    unwired = _declared(cores=[{"core": "row-predicate-filter", "inputs": {"value": "cfg_field0"}}])
    assert TileDefinition.model_validate(unwired).name == "compare"


def test_two_uses_of_one_core_need_labels_and_qualified_config() -> None:
    chain = [
        {
            "core": "row-map-alu",
            "inputs": {"value": "cfg_src_a_field", "bound": "cfg_src_b_field"},
            "output": {"name": "first", "ports": ["cfg_dst_field"]},
        },
        {"core": "row-map-alu", "inputs": {"first": "cfg_src_a_field"}, "output": {"name": "second", "ports": ["cfg_dst_field"]}},
    ]
    with pytest.raises(ValueError, match="distinct labels"):
        TileDefinition.model_validate(_declared(kind="map", cores=chain))
    labelled = [{**chain[0], "as": "one"}, {**chain[1], "as": "two"}]
    with pytest.raises(ValueError, match="must say which core"):
        TileDefinition.model_validate(_declared(kind="map", cores=labelled))
    qualified = [{"expression": 'pl.col("value") < pl.col("bound")', "config": {"two.cfg_op": "SUB"}}]
    declared = TileDefinition.model_validate(_declared(kind="map", cores=labelled, expressions=qualified))
    assert declared.config_for(declared.expressions[0]) == {"one": {}, "two": {"cfg_op": "SUB"}}


def test_the_catalog_composes_and_an_unknown_tile_is_refused() -> None:
    assert tile("compare").name == "compare"
    assert all(name == declared.name for name, declared in loaded_tiles().items())
    with pytest.raises(UnknownTileError, match="known:"):
        tile("no-such-tile")
    # a core's name is not a tile's
    with pytest.raises(UnknownTileError, match="known:"):
        tile("row-predicate-filter")


def _input_ports(name: str) -> set[str]:
    """The input ports the core's module declares."""
    definition = core(name)
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources_for(name) if path.name == definition.source)
    return set(re.findall(r"^\s*input\s+(?:wire\s+)?(?:logic\s+)?(?:signed\s+)?(?:\[[^\]]*\]\s*)*(\w+)\s*,?\s*(?://.*)?$", text, re.MULTILINE))


_USES = [(name, used) for name, declared in loaded_tiles().items() for used in declared.cores]


@pytest.mark.parametrize(("name", "used"), _USES, ids=[f"{name}:{used.label}" for name, used in _USES])
def test_a_tile_names_cores_ports_and_parameters_that_exist(name: str, used) -> None:
    declared = loaded_tiles()[name]
    narrow = core(used.core)
    assert narrow.contract_kind is ContractKind.ROW_STREAM or used.wide is None, f"{used.core} is a wide core; it has no wide sibling"
    names = [used.core]
    if used.wide is not None:
        assert core(used.wide).contract_kind is ContractKind.WIDE_ROW_STREAM
        names.append(used.wide)
    ports = {port for expression in declared.expressions for port in declared.config_for(expression)[used.label]} | set(used.wired_ports())
    parameters = set(used.parameters())
    for core_name in names:
        assert ports <= _input_ports(core_name), f"{core_name} declares no input port {sorted(ports - _input_ports(core_name))}"
        assert parameters <= set(core(core_name).parameters), (
            f"{core_name} declares no parameter {sorted(parameters - set(core(core_name).parameters))}"
        )


@pytest.mark.parametrize(("name", "used"), _USES, ids=[f"{name}:{used.label}" for name, used in _USES])
def test_a_fixed_config_value_is_one_of_the_ports_operators(name: str, used) -> None:
    """A tile fixes a port by the operator's name. The core says which
    operator the port takes, so the name is checked, on the wide sibling
    too."""
    declared = loaded_tiles()[name]
    for expression in declared.expressions:
        for port, value in declared.config_for(expression)[used.label].items():
            for named in value if isinstance(value, tuple) else (value,):
                if isinstance(named, int) or named == LOAD_PHASE_BINDING:
                    continue
                for core_name in filter(None, (used.core, used.wide)):
                    operator = core(core_name).config_operator(port)
                    assert operator is not None, f"{core_name} declares no operator for {port}, which {name} fixes to {named!r}"
                    assert named in operator.__members__, f"{named!r} is not a {operator.__name__}"

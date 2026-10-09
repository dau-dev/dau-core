"""Frozen is not enough: the two holes in it, and what closes them.

Every model here is frozen and validates at construction. ``model_copy`` goes
around the validators, and a ``dict`` on a frozen model goes around the
freeze. These tests pin that both are closed, and that the mechanism does not
change what a model means.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from dau_core.capability_contract import capability_contract
from dau_core.composition import CompositionError, LaneSpec, ScanCompositionSpec, TileSpec, canonical_spec_json, spec_hash
from dau_core.cores import ResourceMeasurement
from dau_core.models import CheckedModel, FrozenMapping
from dau_core.operators import RowField
from dau_core.registry import core


def _spec() -> ScanCompositionSpec:
    return ScanCompositionSpec(
        name="checked",
        lanes=(
            LaneSpec(
                tile=TileSpec(core="field-sum-aggregation", config={"cfg_field_mask": "4'b0010"}),
                partition=TileSpec(core="pair-key-filter", config={"cfg_key_mask": "32'd3", "cfg_key_match": "32'd1"}),
            ),
        ),
    )


def test_a_copied_model_is_refused_at_the_next_boundary() -> None:
    """``model_copy`` skips every validator, so the copy itself cannot refuse.
    The boundary it is handed to does: a parent revalidates its children, and
    ``check()`` revalidates on demand."""
    measurement = ResourceMeasurement(part="xc7a200tfbg484-2", clock_ns=8.0, lut=10, ff=10, bram36=0, dsp=0, timing="met", wns_ns=0.1)
    # a copy pydantic never looked at: the slack now disagrees with the status
    tampered = measurement.model_copy(update={"wns_ns": -1.0})
    assert tampered.wns_ns == -1.0  # construction would have refused this

    with pytest.raises(ValidationError, match="disagrees with WNS"):
        tampered.check()
    # the parent is the boundary: it revalidates the child it was handed
    with pytest.raises(ValidationError, match="disagrees with WNS"):
        core("field-sum-aggregation").replace(resources=(tampered,))


def test_replace_is_the_validated_copy() -> None:
    measurement = ResourceMeasurement(part="xc7a200tfbg484-2", clock_ns=8.0, lut=10, ff=10, bram36=0, dsp=0, timing="met", wns_ns=0.1)
    assert measurement.replace(lut=20).lut == 20
    with pytest.raises(ValidationError, match="disagrees with WNS"):
        measurement.replace(wns_ns=-1.0)
    # check() on an untouched model returns an equal one and runs the validators
    assert measurement.check() == measurement


def test_a_frozen_model_holds_no_mutable_mapping() -> None:
    """A frozen model whose ``config`` is a plain dict is not frozen: whoever
    holds the model can rewrite the binding the design was priced under.

    There is no mutating method to refuse: the field holds a ``Mapping``, not
    a ``dict`` subclass, so nothing is inherited that could write. A subclass
    would have been the weaker design -- ``dict.__setitem__(m, k, v)`` reaches
    past any override -- so that bypass is pinned here too.
    """
    tile = TileSpec(core="field-sum-aggregation", config={"cfg_field_mask": "4'b0010"})
    assert isinstance(tile.config, FrozenMapping)
    for method in ("__setitem__", "__delitem__", "update", "pop", "popitem", "clear", "setdefault"):
        assert not hasattr(tile.config, method), method
    # through getattr because a type checker refuses the call outright, which
    # is the other half of the answer
    with pytest.raises(TypeError, match="requires a 'dict' object"):
        getattr(dict, "__setitem__")(tile.config, "cfg_field_mask", "4'b1111")  # noqa: B009
    # merging builds a new mapping and leaves this one alone
    merged = tile.config | {"cfg_other": "1"}
    assert set(merged) == {"cfg_field_mask", "cfg_other"} and set(tile.config) == {"cfg_field_mask"}
    assert isinstance(core("streaming-top-k").parameters, FrozenMapping)


def test_a_read_only_mapping_still_behaves_like_the_dict_it_was() -> None:
    """The freeze is invisible to everything that reads: equality against a
    plain dict, membership, serialization, and the hash a frozen model needs."""
    tile = TileSpec(core="field-sum-aggregation", config={"cfg_field_mask": "4'b0010"})
    assert tile.config == {"cfg_field_mask": "4'b0010"}
    assert dict(tile.config) == {"cfg_field_mask": "4'b0010"}
    assert "cfg_field_mask" in tile.config
    assert tile.model_dump()["config"] == {"cfg_field_mask": "4'b0010"}
    assert json.loads(tile.model_dump_json())["config"] == {"cfg_field_mask": "4'b0010"}
    assert hash(tile) == hash(TileSpec(core="field-sum-aggregation", config={"cfg_field_mask": "4'b0010"}))
    # an unset mapping field is frozen too: a default is never validated, so
    # its factory is what has to produce the read-only form
    assert isinstance(tile.params, FrozenMapping)
    assert hash(TileSpec(core="field-sum-aggregation")) == hash(TileSpec(core="field-sum-aggregation"))


def test_the_canonical_form_and_the_cache_key_are_unchanged() -> None:
    """The design cache is keyed on this text. A change to how a model holds
    its mappings must not move it, so the pinned-hash fixture in
    test_composition.py keeps meaning what it meant."""
    spec = _spec()
    text = canonical_spec_json(spec)
    assert json.loads(text)["lanes"][0]["tile"]["config"] == {"cfg_field_mask": "4'b0010"}
    assert spec_hash(spec) == spec_hash(ScanCompositionSpec.model_validate(json.loads(text)))


def test_the_composition_boundaries_refuse_a_copied_spec() -> None:
    """Every entry point that reads a spec revalidates it first, so a spec
    that was copied past its validators cannot be hashed, priced or
    advertised."""
    from dau_core.composition import estimated_resources, throughput_rows_per_cycle

    tampered = _spec().model_copy(update={"data_width": 7})  # not a width any tier reads
    for call in (
        lambda: canonical_spec_json(tampered),
        lambda: estimated_resources(tampered, part="xc7a200tfbg484-2", clock_ns=8.0),
        lambda: throughput_rows_per_cycle(tampered),
        lambda: capability_contract(tampered),
    ):
        # the composition validator raises its own error type, which pydantic
        # does not wrap; either way the call refuses rather than reading it
        with pytest.raises((ValidationError, CompositionError), match="data_width"):
            call()


def test_a_polymorphic_field_keeps_its_subclass() -> None:
    """``CoreGenerator`` is a field type, so it must NOT revalidate instances:
    a subclass handed to ``CoreDefinition.generator`` would be validated
    against the base and lose its own fields. The generated cores in the
    registry are the proof -- they load -- and check() on the concrete
    subclass is what validates one."""
    definition = core("fixed-point-exp")
    assert definition.generator is not None
    assert type(definition.generator).__name__ == "ExpTileGenerator"
    assert definition.generator.check() == definition.generator
    assert "revalidate_instances" not in type(definition.generator).model_config


def test_every_model_validates_at_use() -> None:
    """The guard: a dau-core model that is not checkable is a model whose
    copies nothing refuses."""
    import importlib
    import inspect
    import pkgutil

    from ccflow import BaseModel

    import dau_core

    missing: list[str] = []
    seen: set[type] = set()
    for module in pkgutil.walk_packages(dau_core.__path__, "dau_core."):
        if ".tests" in module.name:
            continue
        loaded = importlib.import_module(module.name)
        for _name, cls in inspect.getmembers(loaded, inspect.isclass):
            if not issubclass(cls, BaseModel) or cls is BaseModel or cls.__module__ != module.name or cls in seen:
                continue
            seen.add(cls)
            if not hasattr(cls, "check") or not cls.model_config.get("frozen"):
                missing.append(f"{module.name}.{cls.__name__}")
    assert seen, "the walk found no models"
    assert not missing, f"models that are not frozen-and-checkable: {missing}"


def test_the_base_refuses_an_unfrozen_subclass() -> None:
    """A subclass that unfreezes itself is the quiet way back to a mutable
    model, so the base's config is what a new model inherits."""
    assert CheckedModel.model_config["frozen"] is True
    assert CheckedModel.model_config["revalidate_instances"] == "always"


def test_a_copied_generator_cannot_render() -> None:
    """A generator is a field type, so nothing revalidates it on the way in.
    ``render()`` is therefore its own boundary: a copied operating point must
    not reach HDL, because that HDL's digest would then identify it."""
    from dau_core.exp_generator import ExpTileGenerator

    definition = core("fixed-point-exp")
    generator = definition.generator
    # the declared field type is the base, so the subclass is named here: the
    # datapath is the subclass's own field, which is the whole point below
    assert isinstance(generator, ExpTileGenerator)
    # degree 0 is no polynomial at all; construction refuses it
    tampered = generator.model_copy(update={"datapath": generator.datapath.model_copy(update={"degree": 0})})

    with pytest.raises(ValidationError):
        tampered.check()
    with pytest.raises(ValidationError):
        tampered.render()
    with pytest.raises(ValidationError):
        definition.replace(generator=tampered).render()


def test_a_golden_refuses_a_copied_config() -> None:
    """The goldens are what hardware is compared against, so a config that
    construction would have refused must not produce a reference answer. The
    check runs once per call, outside the row loop."""
    from dau_core.operators import MapOp, RowMapConfig, map_row_fields

    config = RowMapConfig(op=MapOp.ADD, dst=RowField.VALUE, src_a=RowField.VALUE, src_b=RowField.AUX0)
    assert map_row_fields([(1, 2, 3, 0)], config) == [(1, 5, 3, 0)]

    with pytest.raises(ValidationError):
        map_row_fields([(1, 2, 3, 0)], config.model_copy(update={"shift": 99}))


def test_staging_the_hdl_refuses_a_copied_spec() -> None:
    """``core_names`` decides which sources a build compiles. A spec copied
    past its validators must not make that decision."""
    with pytest.raises((ValidationError, CompositionError), match="data_width"):
        _spec().model_copy(update={"data_width": 7}).core_names()


def test_a_record_batch_is_validated_against_checked_limits() -> None:
    from dau_core.stream import DEFAULT_RECORD_BATCH_LIMITS, RecordBatchLimits

    assert DEFAULT_RECORD_BATCH_LIMITS.check() == DEFAULT_RECORD_BATCH_LIMITS
    assert isinstance(DEFAULT_RECORD_BATCH_LIMITS, RecordBatchLimits)

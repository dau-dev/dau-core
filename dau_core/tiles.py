"""Tile catalog models: what a query can ask the device for.

A tile is a logical operation. It states the polars shapes it answers and
the chain of cores that computes them, with the config port each input
arrives on and the field each core writes. A core is one HDL building block
(:class:`dau_core.cores.CoreDefinition`); the same core serves several
tiles, and a tile may chain several cores.

The catalog lives in dau-core's hydra config tree
(``dau_core/config/dau-tiles/tiles.yaml``), composed into the ccflow
``ModelRegistry`` at ``/dau-tiles/<name>``. A contributing package declares
its tiles in its own composed group, as it does its cores.

How lanes are arranged is not a tile: replication behind the key
dispatcher, the range partitioner ahead of sorter lanes and the front
unpacker are chosen when a design is priced.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Mapping
from enum import Enum
from typing import Annotated, Any

from pydantic import Field, StrictInt, model_validator

from .models import CheckedModel, ReadOnly

__all__ = ("InputSource", "TileCore", "TileDefinition", "TileExpression", "TileInput", "TileKind", "TileOutput")


# a config port's fixed value: an operator's name, an integer, or the values
# the staging chooses between
ConfigValue = str | StrictInt | tuple[str | StrictInt, ...]


def _column_names(node: Any) -> list[str]:
    """Every column a serialized polars plan or expression names, in tree
    order."""
    if isinstance(node, dict):
        if set(node) == {"Column"} and isinstance(node["Column"], str):
            return [node["Column"]]
        return [name for value in node.values() for name in _column_names(value)]
    if isinstance(node, list):
        return [name for value in node for name in _column_names(value)]
    return []


class TileKind(Enum):
    """What a tile does to the rows of a lane."""

    FILTER = "filter"  # keeps some rows
    MAP = "map"  # derives a column
    AGGREGATE = "aggregate"  # ends the lane in one record
    GROUPED_AGGREGATE = "grouped-aggregate"  # ends the lane in one record per group
    JOIN = "join"  # adds columns from a loaded table
    ORDER = "order"  # ends the lane in rows, in key order
    WINDOW = "window"  # ends the lane in one record per row over a trailing window


class InputSource(Enum):
    """An input a core takes without a config port."""

    ROW_KEY = "row-key"  # the row's key field; the core has no field select for it
    ROW_VALUE = "row-value"  # the row's value field
    LOAD_IMAGE = "load-image"  # streamed to the core in the LOAD phase, before the scan
    PREDICATE = "predicate"  # configured by whichever filter tile answers the condition


class TileInput(CheckedModel):
    """Where a core takes one input.

    ``ports`` are config ports: field selects for a column, value ports for
    a literal. A literal list or a column tested more than once names one
    port per use. ``mask`` is a config port with one bit per row field.
    ``shift`` and ``width`` place a column packed into a field with others:
    the config port taking its bit offset and the module parameter fixing
    its width, beside the field select in ``ports`` when the core has one.
    ``parameter`` is a module parameter the value fixes when the design is
    built. ``source`` is an input that arrives without a config port.

    In the catalog a single port is written as its name and several as a
    list (:meth:`TileCore.taken`).
    """

    ports: tuple[str, ...] = ()
    mask: str | None = None
    shift: str | None = None
    width: str | None = None
    parameter: str | None = None
    source: InputSource | None = None

    @model_validator(mode="after")
    def _one_way_in(self) -> TileInput:
        packed = self.shift is not None or self.width is not None
        ways = [bool(self.ports) or packed, self.mask is not None, self.parameter is not None, self.source is not None]
        if sum(ways) != 1:
            raise ValueError("an input arrives one way: on ports (packed or not), in a mask, as a parameter, or from a source")
        return self

    def config_ports(self) -> tuple[str, ...]:
        """The config ports this input is wired to."""
        return (*self.ports, *(port for port in (self.mask, self.shift) if port is not None))

    def parameters(self) -> tuple[str, ...]:
        """The module parameters this input sets."""
        return tuple(parameter for parameter in (self.width, self.parameter) if parameter is not None)


class TileOutput(CheckedModel):
    """What a core hands on: a name later cores (or the query) read it by,
    and the config ports selecting the row fields it is written to."""

    name: str = Field(min_length=1)
    ports: tuple[str, ...] = Field(min_length=1)


class TileCore(CheckedModel):
    """One core in a tile's chain.

    ``wide`` is the core doing the same job on the wide row stream, with the
    same config ports. ``as`` tells two uses of one core apart
    (:attr:`label`). ``inputs`` maps a placeholder of the tile's shapes, or
    an earlier core's output, to where this core takes it: a config port's
    name, a list of them, or a :class:`TileInput`. ``config`` holds config
    ports at a fixed value, by the operator's name or as an integer; a list
    is the values the staging chooses between. ``derived`` are config ports
    set from how the table is staged and ``params`` module parameters sized
    from the staged data, each with what sets it.
    """

    core: str = Field(min_length=1)
    wide: str | None = None
    as_: str | None = Field(default=None, alias="as")
    inputs: Annotated[Mapping[str, str | tuple[str, ...] | TileInput], *ReadOnly]
    output: TileOutput | None = None
    config: Annotated[Mapping[str, ConfigValue], *ReadOnly] = {}
    derived: Annotated[Mapping[str, str], *ReadOnly] = {}
    params: Annotated[Mapping[str, str], *ReadOnly] = {}

    @property
    def label(self) -> str:
        """What the tile calls this use of the core: ``as``, or the core's
        name."""
        return self.as_ or self.core

    def taken(self, name: str) -> TileInput:
        """Where this core takes the input called ``name``."""
        bound = self.inputs[name]
        if isinstance(bound, TileInput):
            return bound
        return TileInput(ports=(bound,) if isinstance(bound, str) else tuple(bound))

    def wired_ports(self) -> tuple[str, ...]:
        """The config ports wired to an input or the output, or derived
        from the staging."""
        ports = [port for name in self.inputs for port in self.taken(name).config_ports()]
        if self.output is not None:
            ports.extend(self.output.ports)
        return (*ports, *self.derived)

    def parameters(self) -> tuple[str, ...]:
        """The module parameters this use of the core sets."""
        return (*(parameter for name in self.inputs for parameter in self.taken(name).parameters()), *self.params)


class TileExpression(CheckedModel):
    """One polars shape a tile answers, as polars source text.

    ``expression`` is an expression over placeholder columns
    (``'pl.col("value") < pl.col("bound")'``). ``frame`` is a query over a
    placeholder frame, for a shape an expression cannot state: a group-by,
    a join against ``other``, a sort or a row limit
    (``'frame.sort("key", descending=True).head(k)'``). Either may read the
    tile's arguments as bare names. ``config`` holds the config values this
    shape selects (the operator), keyed by port, or by ``label.port`` in a
    tile with more than one core.
    """

    expression: str | None = None
    frame: str | None = None
    config: Annotated[Mapping[str, ConfigValue], *ReadOnly] = {}

    @model_validator(mode="after")
    def _one_form(self) -> TileExpression:
        if (self.expression is None) == (self.frame is None):
            raise ValueError("a shape is an expression or a frame query, one of the two")
        return self

    @property
    def text(self) -> str:
        """The shape's polars source."""
        return self.expression if self.expression is not None else str(self.frame)

    def evaluate(self, **names: Any) -> Any:
        """The shape as polars builds it: a ``pl.Expr``, or a ``LazyFrame``
        for a frame query. ``names`` binds what the text reads besides
        ``pl``: the frames and the arguments. Needs polars installed."""
        import polars as pl

        # catalog text is code, as a hydra `_target_` is: evaluated with no
        # builtins and only the names given
        return eval(self.text, {"__builtins__": {}}, {"pl": pl, **names})

    def plan_json(self, arguments: tuple[str, ...] = ()) -> dict[str, Any]:
        """The shape as polars serializes it, over empty frames and with
        every argument at a sample value."""
        import json

        import polars as pl

        frames = {"frame": pl.LazyFrame(), "other": pl.LazyFrame()} if self.frame is not None else {}
        built = self.evaluate(**frames, **dict.fromkeys(arguments, 2))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # the JSON plan format is deprecated upstream
            text = built.serialize(format="json") if self.frame is not None else built.meta.serialize(format="json")
        return json.loads(text)

    def placeholders(self, arguments: tuple[str, ...] = ()) -> tuple[str, ...]:
        """Every placeholder the shape names: its columns in tree order,
        then the arguments it reads."""
        read = tuple(name for name in arguments if re.search(rf"(?<![\w.\"']){re.escape(name)}(?![\w\"'(])", self.text))
        return tuple(dict.fromkeys((*_column_names(self.plan_json(arguments)), *read)))


class TileDefinition(CheckedModel):
    """One logical operation: the polars shapes it answers and the cores
    that compute them.

    Every shape of a tile runs on the same chain; they differ in the config
    values they select. Two chains answering the same shape are two tiles,
    and the compiler chooses between them.

    ``literals`` are the placeholder columns that match a literal in the
    query; any other placeholder column matches a column, or a
    sub-expression another tile computes first. ``arguments`` are the bare
    names a shape reads where polars takes a number (a window size, a row
    limit). ``result`` says what a lane ending in this tile returns to the
    host and anything the host finishes.

    ``scale_invariant`` is a promise a compiler needs before it runs a row
    expression over fixed-point columns: ``f(k*x, k*s...) == k*f(x, s...)``
    for k > 0, so computing on the staged integers and decoding gives what
    polars computes on the values. True of a clip, false of a product.
    """

    name: str = Field(min_length=1)
    kind: TileKind
    description: str = Field(min_length=1)
    literals: tuple[str, ...] = ()
    arguments: tuple[str, ...] = ()
    expressions: tuple[TileExpression, ...] = Field(min_length=1)
    cores: tuple[TileCore, ...] = Field(min_length=1)
    result: str | None = None
    scale_invariant: bool = False

    def core_named(self, label: str) -> TileCore:
        for used in self.cores:
            if used.label == label:
                return used
        raise KeyError(label)

    def placeholders(self, expression: TileExpression) -> tuple[str, ...]:
        """The placeholders ``expression`` names, arguments included."""
        return expression.placeholders(self.arguments)

    def config_for(self, expression: TileExpression) -> dict[str, dict[str, ConfigValue]]:
        """The fixed config values of each core, by label, when the tile
        answers ``expression``."""
        values: dict[str, dict[str, ConfigValue]] = {used.label: dict(used.config) for used in self.cores}
        for key, value in expression.config.items():
            label, port = self._config_target(key)
            values[label][port] = value
        return values

    def _config_target(self, key: str) -> tuple[str, str]:
        label, separator, port = key.rpartition(".")
        if not separator:
            if len(self.cores) != 1:
                raise ValueError(f"config {key!r} must say which core it sets (label.port): the tile has {len(self.cores)} cores")
            return self.cores[0].label, port
        if label not in {used.label for used in self.cores}:
            raise ValueError(f"config {key!r} names no core of the tile")
        return label, port

    @model_validator(mode="after")
    def _chain_is_wired(self) -> TileDefinition:
        labels = [used.label for used in self.cores]
        if len(set(labels)) != len(labels):
            raise ValueError(f"two uses of one core need distinct labels (as): {labels}")
        for expression in self.expressions:
            for label, values in self.config_for(expression).items():
                wired = set(values) & set(self.core_named(label).wired_ports())
                if wired:
                    raise ValueError(f"{label}: {sorted(wired)} are wired or derived and cannot also hold a fixed value")
        if self.kind is TileKind.MAP and self.cores[-1].output is None:
            raise ValueError("a map tile's last core writes the derived field: it needs an output")
        if self.kind in (TileKind.AGGREGATE, TileKind.GROUPED_AGGREGATE, TileKind.ORDER, TileKind.WINDOW) and not self.result:
            raise ValueError(f"a {self.kind.value} tile ends its lane: it says what the lane returns (result)")
        if set(self.literals) & set(self.arguments):
            raise ValueError(f"{sorted(set(self.literals) & set(self.arguments))} are declared both a literal and an argument")
        try:
            placeholders = set().union(*(self.placeholders(expression) for expression in self.expressions))
        except ImportError:
            # no polars here: a compiler, which has it, checks the wiring
            return self
        except Exception as error:
            raise ValueError(f"a shape is not polars source: {error}") from error
        for kind, names in (("literals", self.literals), ("arguments", self.arguments)):
            if not set(names) <= placeholders:
                raise ValueError(f"{kind} {sorted(set(names) - placeholders)} are not placeholders of any shape")
        available = set(placeholders)
        consumed: set[str] = set()
        for used in self.cores:
            unknown = set(used.inputs) - available
            if unknown:
                raise ValueError(f"{used.label}: inputs {sorted(unknown)} are neither placeholders nor an earlier core's output")
            consumed |= set(used.inputs)
            if used.output is not None:
                available.add(used.output.name)
        if not placeholders <= consumed:
            raise ValueError(f"placeholders {sorted(placeholders - consumed)} reach no core")
        return self

# Tile catalog

A tile is a logical operation a query can ask the device for. It states the
polars shapes it answers and the chain of cores that computes them. Tiles
are declared in `dau_core/config/dau-tiles/tiles.yaml`, one
`_target_: dau_core.tiles.TileDefinition` each, and loaded into the ccflow
`ModelRegistry` at `/dau-tiles/<name>`.

Tiles and cores are many to many. `row-predicate-filter` serves the
`compare`, `between`, `in-list` and `in-range` tiles; `compare-columns`
chains `row-map-alu` into `row-predicate-filter`. Two chains that answer the
same shape are two tiles (`in-list`, `in-range` and `in-set` all answer
`is_in`), and a query compiler picks one from how the table is staged.

How lanes are arranged is not a tile. Replication behind the key
dispatcher, the range partitioner ahead of sorter lanes and the front
unpacker are chosen when a design is priced.

## `TileDefinition`

| Field         | Type                         | Description                                                                                                                                            |
| ------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `name`        | `str`                        | Registry key.                                                                                                                                          |
| `kind`        | `TileKind`                   | `filter`, `map`, `aggregate`, `grouped-aggregate`, `join`, `order` or `window`.                                                                        |
| `description` | `str`                        | What the tile does and what it needs from the staging.                                                                                                 |
| `literals`    | `tuple[str, ...]`            | Placeholder columns that match a literal in the query. Any other placeholder column matches a column, or a sub-expression another tile computes first. |
| `arguments`   | `tuple[str, ...]`            | Bare names a shape reads where polars takes a number (a window size, a row limit).                                                                     |
| `expressions` | `tuple[TileExpression, ...]` | The polars shapes the tile answers. All of them run on the same chain.                                                                                 |
| `cores`       | `tuple[TileCore, ...]`       | The chain, in order.                                                                                                                                   |
| `result`      | `str \| None`                | What a lane ending in the tile returns to the host, and anything the host finishes. Required for the kinds that end a lane.                            |

## `TileExpression`

A shape is polars source text, in one of two forms.

| Field        | Type          | Description                                                                                                                            |
| ------------ | ------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `expression` | `str \| None` | An expression over placeholder columns: `pl.col("value") < pl.col("bound")`.                                                           |
| `frame`      | `str \| None` | A query over a placeholder `frame`, for a shape an expression cannot state: a group-by, a join against `other`, a sort or a row limit. |
| `config`     | mapping       | The config values this shape selects, usually the operator. Keyed by port, or by `label.port` in a tile with more than one core.       |

## `TileCore`

| Field     | Type                 | Description                                                                                                                    |
| --------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `core`    | `str`                | The core's registry name.                                                                                                      |
| `wide`    | `str \| None`        | The core doing the same job on the wide row stream, on the same config ports. A tile that needs more than one lane has none.   |
| `as`      | `str \| None`        | A label telling two uses of one core apart. Defaults to the core's name.                                                       |
| `inputs`  | mapping              | Placeholder, or an earlier core's output, to where the core takes it (below).                                                  |
| `output`  | `TileOutput \| None` | The name of what the core produces and the config `ports` selecting the fields it is written to. A filter has none.            |
| `config`  | mapping              | Config ports held at a fixed value, by the operator's name or as an integer. A list is the values the staging chooses between. |
| `derived` | mapping              | Config ports set from how the table is staged, each with what sets it.                                                         |
| `params`  | mapping              | Module parameters sized from the staged data, each with what sizes it.                                                         |

An input arrives one way:

| Form                       | Meaning                                                                                                                                                       |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `cfg_field0`               | One config port: a field select for a column, a value port for a literal.                                                                                     |
| `[cfg_field0, cfg_field1]` | Several ports, for a column tested more than once or a literal list.                                                                                          |
| `{mask: cfg_field_mask}`   | A config port with one bit per row field.                                                                                                                     |
| `{shift: ..., width: ...}` | A column packed into a field with others: the port taking its bit offset and the parameter fixing its width.                                                  |
| `{parameter: WINDOW}`      | A module parameter the value fixes when the design is built.                                                                                                  |
| `{source: row-key}`        | No config port: `row-key`, `row-value`, `load-image` (streamed in the LOAD phase) or `predicate` (configured by whichever filter tile answers the condition). |

## Accessors

```python
from dau_core.registry import loaded_tiles, tile

declared = tile("compare-columns")
print([used.core for used in declared.cores])
print(declared.config_for(declared.expressions[2]))
print(declared.placeholders(declared.expressions[2]))
print(len(loaded_tiles()))
```

```
['row-map-alu', 'row-predicate-filter']
{'row-map-alu': {'cfg_op': 'SUB'}, 'row-predicate-filter': {'cfg_constant0': 0, 'cfg_op0': 'LT'}}
('left', 'right')
22
```

## What is checked

A `TileDefinition` checks its own wiring where it is declared: every
placeholder reaches a core, every input is a placeholder or an earlier
core's output, and no port is both wired and fixed. The test suite checks
the composed catalog against the registry and the HDL: each named core
exists, and each config port and parameter is one its module declares.

## See also

- [`CoreDefinition` fields](core-definition.md): what a core declares.

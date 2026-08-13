# Why the registry is configuration

There is no Python list of cores anywhere in this package. The palette is a
Hydra config tree — `dau_core/config/dau-core/cores.yaml`, one
`_target_: dau_core.cores.CoreDefinition` entry per HDL building block —
composed into the ccflow `ModelRegistry` at `/dau-core/<core-name>`. The
Python accessors in `dau_core.registry` read that composition; they do not
own it.

That reads like a packaging preference. It is not. It decides who is
allowed to change the palette, and the answer the project wanted was
"anyone composing the stack, without forking this package".

## A Python list makes the palette private

If the registry were a module-level dict, every fact about every tile would
live behind an import. Changing a tile's declared clock, adding a tile,
retargeting an envelope to a different part — each is an edit to installed
library source. A consuming application could monkey-patch, vendor a fork,
or do without. All three are worse than the problem.

As configuration, the same operations are overrides. An application
composes `/dau-core` with its own overlay, changes what it needs, and the
accessors return the composed result. `loaded_cores()` reads the
subregistry a consumer has already composed and never reloads or overwrites
it; the packaged defaults load only when no `/dau-core` subregistry is
present. The registry an application has composed *is* the registry.

## Discovery without an import

The package advertises its config tree through a `hydra.lernaplugins` entry
point (`dau-core = "pkg:dau_core.config"`). Anything composing Hydra
configs finds the cores on the shared search path without importing
`dau_core` at all. The build layer, the frontend, and the integration home
each compose the same tree rather than each holding a copy of the palette,
and a version skew between them shows up as a composition error rather than
as two disagreeing lists.

## Two surfaces, deliberately apart

Four modules make up the tile-*authoring* surface: `dau_core.cores` (the
models — `CoreDefinition`, `IngestRate`, `StorageIntent`,
`ParameterSpec`), `dau_core.design`, `dau_core.generators`, and
`dau_core.stream` (the row-stream ABI). `dau_core.registry` is not among
them: it holds read-through views over the tiles *this* package ships.

A third party declaring a tile needs the authoring surface; it must never
need the accessors, because the accessors know only about tiles that live
in this repository. `dau_core/tests/test_public_tile_surface.py` enforces
the separation statically, enumerating exactly which imports the surface
may make — every added name is a module that would have to travel with the
surface if it were extracted or published.

## What the discipline costs

Facts that would be one Python expression become a yaml entry plus a model
field, and a typo lands as a composition failure rather than an
`AttributeError` at the call site. Reading the palette means reading a
3,600-line yaml file rather than skimming a module.

The trade is deliberate. A palette that can only be changed by editing the
library is a palette with exactly one author, and the whole point of a
registry is that it has more than one.

## See also

- [How to add an operator tile](Add-an-Operator-Tile) — the sequence a new
  entry goes through.
- [`CoreDefinition` fields](Core-Definition) — what an entry may declare.
- [Why resource envelopes are measurements that refuse](Why-Resources-Are-Measured) —
  why the costliest field in an entry is the one that raises.

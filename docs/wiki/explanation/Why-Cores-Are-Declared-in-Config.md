# Why cores are declared in config, not code

`dau-core` has no Python list of cores. The palette is a Hydra config tree,
`dau_core/config/dau-core/cores.yaml`, with one
`_target_: dau_core.cores.CoreDefinition` entry per HDL building block, and
it is composed into the ccflow `ModelRegistry` at `/dau-core/<core-name>`.
The accessors in `dau_core.registry` read that composition; they do not own
it.

The reason is about who can change the palette. The goal was for anyone
composing the stack to be able to change it without forking this package.

## A Python list would keep the palette inside the package

If the registry were a module-level dict, every fact about every tile would
sit behind an import. Changing a tile's declared clock, adding a tile, or
pointing an envelope at a different part would each mean editing installed
library source. A consuming application would have to monkey-patch, vendor
a fork, or go without the change.

With the palette in config, those are overrides. An application composes
`/dau-core` with its own overlay, changes what it needs, and the accessors
return the composed result. `loaded_cores()` reads a subregistry the
application has already composed and never reloads or overwrites it; the
packaged defaults load only when no `/dau-core` subregistry exists. Whatever
the application composed is the registry.

## Discovery without an import

The package advertises its config tree through a `hydra.lernaplugins` entry
point (`dau-core = "pkg:dau_core.config"`). Anything composing Hydra configs
finds the cores on the shared search path without importing `dau_core`. The
build layer, the frontend, and the integration package all compose the same
tree instead of each keeping a copy of the palette, so a version mismatch
between them shows up as a composition error instead of two lists that
disagree.

## The authoring surface and the accessors are kept separate

A third party adding a tile needs the models, not the accessors. The
authoring surface is six modules: `dau_core.cores` (`CoreDefinition`,
`IngestRate`, `StorageIntent`, `ParameterSpec`), `dau_core.design`,
`dau_core.element_types`, `dau_core.generators`, `dau_core.models`, and
`dau_core.stream` (the row-stream ABI). `dau_core.registry` is not part of
it, because the accessors only know about the tiles this package ships.

`dau_core/tests/test_public_tile_surface.py` checks the separation
statically by listing exactly which imports the surface may make. Each
module added to that list is one more module that would have to move with
the surface if it were ever published on its own.

## What it costs

A fact that would be one Python expression becomes a yaml entry plus a model
field, and a typo shows up as a composition failure instead of an
`AttributeError` where it is used. Reading the palette means reading a long
yaml file instead of skimming a module.

That is the price of letting more than one party edit the palette. A
registry that only this package can change has one author, and a registry
exists so that it can have more than one.

## See also

- [How to add an operator tile](Add-an-Operator-Tile): the steps a new entry
  goes through.
- [`CoreDefinition` fields](Core-Definition): what an entry can declare.
- [Why resource costs are measured](Why-Resources-Are-Measured): why the
  most expensive field in an entry raises instead of guessing.

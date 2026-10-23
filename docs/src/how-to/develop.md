# Develop and contribute

Contributions are welcome under the
[Apache 2.0 license](https://github.com/dau-dev/dau-core/blob/main/LICENSE).
Every commit needs a Developer Certificate of Origin sign-off
(`git commit -s`), which a GitHub app checks on each pull request.

## Set up

```bash
git clone https://github.com/dau-dev/dau-core.git
cd dau-core
make develop
```

`make develop` installs the package in editable mode with its development
dependencies, which include `dau-build` and `dau-sim` for the cross-repo
guards and the cocotb benches. `make` with no arguments lists the targets.

The HDL tests need `verilator` on `PATH`. Nothing in this repository needs
Vivado; measuring a resource envelope does, and that runs through `dau-build`
on a synthesis host (see
[how to measure a resource envelope](measure-a-resource-envelope.md)).

## Lint, type-check, test

```bash
make lint       # ruff, mdformat and codespell
make checks     # distribution check and ty
make coverage   # the suite, without the HDL compile tier
make test-hdl   # elaboration, cocotb benches and raw testbenches
```

The HDL tests come in two tiers. Elaboration runs `verilator --lint-only`
over every declared parameterization in seconds, so `make coverage` includes
it. The compile tier builds and simulates every bench and takes around half
an hour, so it runs only under `make test-hdl`. Both skip when `verilator` is
missing; set `DAU_REQUIRE_VERILATOR=1` to make a missing `verilator` fail
instead, as CI does.

Run `make lint`, `make checks` and `make coverage` before opening a pull
request, and `make test-hdl` when the change touches HDL. The Python blocks in
the README and on these pages run as tests, so an example that stops working
fails the suite.

## Documentation

The pages under `docs/src` and the README make up the documentation site. It
is built with [yardang](https://github.com/python-project-templates/yardang)
from `[tool.yardang]` in `pyproject.toml`, which lists the pages in order, and
the docs workflow publishes it to GitHub Pages after each green build on
`main`. To build it locally:

```bash
uv pip install "yardang[themes]"
yardang build
```

The site lands in `docs/html`.

## Pull requests

Work on a fork and open a pull request against `main`. A draft is fine while
the change is in progress. Squash into logical commits, all signed. Open an
issue first for anything larger than a fix, and use the discussions page for
questions.

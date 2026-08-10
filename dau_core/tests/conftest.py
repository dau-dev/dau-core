"""Session setup shared by the dau-core suites."""

from __future__ import annotations

import pytest


@pytest.fixture(scope="session", autouse=True)
def _render_generated_cores():
    """Materialize generated cores once for the whole session.

    A generated core refuses its source path until rendered, which is the
    point -- but every suite that walks the registry (contract conformance,
    elaboration) needs the sources present. Rendering here keeps that refusal
    strict in production while letting the suites see a complete palette, and
    it exercises the render path on every run.
    """
    from dau_core.cores import loaded_cores

    for definition in loaded_cores().values():
        if definition.generator is not None:
            definition.render()

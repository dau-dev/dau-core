"""Cores whose HDL is RENDERED rather than checked in.

Most tiles are a hand-written ``.sv`` beside their registry entry. A few
cannot be: this one needs 256 entries of ``2**(j/256)`` to 52 bits, and
SystemVerilog has no way to compute a transcendental at elaboration. The
constants have to come from somewhere, and the choice is between committing
a generated file that silently drifts from the parameters that produced it,
or rendering it from those parameters on demand.

Rendering is the same shape as everything else here: the generator is a
ccflow ``CallableModel`` reached through the registry, its parameters are
config, and the rendered text is a typed result. A core declares one in its
registry entry and dau-build renders it before synthesis; nothing imports a
generator directly, so the public build package can drive a private core's
generator without importing it.

The rule that makes this safe is in :meth:`CoreDefinition.source_path`: a
generated core REFUSES to hand out a path until it has been rendered. The
alternative -- rendering implicitly on first read -- hides which parameters
produced the file sitting on disk, and that file is what gets synthesized.
"""

# NOTE: no `from __future__ import annotations` — ccflow's Flow.call
# inspects the real annotation objects on __call__

from collections.abc import Mapping
from pathlib import Path

from ccflow import CallableModel, Flow, GenericContext, ResultBase
from pydantic import ConfigDict

__all__ = ("CoreGenerator", "RenderContext", "RenderedCore")

# A generator takes no runtime input -- everything it needs is configuration
# -- but ccflow calls with a context, so this names the empty one.
RenderContext = GenericContext[str]


class RenderedCore(ResultBase):
    """One rendered SystemVerilog source and the parameters behind it.

    ``parameters`` is carried so the rendered text can be traced back to the
    configuration that produced it; a generated file with no record of its
    inputs is the thing this design exists to avoid.
    """

    model_config: ConfigDict = ConfigDict(frozen=True)

    module: str
    source_name: str
    text: str
    parameters: Mapping[str, int] = {}

    def write(self, into: Path) -> Path:
        """Materialize under ``into``, returning the written path."""
        into.mkdir(parents=True, exist_ok=True)
        path = into / self.source_name
        path.write_text(self.text, encoding="utf-8")
        return path


class CoreGenerator(CallableModel):
    """Base for anything that renders a core's HDL from configuration.

    Subclasses carry their parameters as model fields, so an operating point
    is chosen the same way every other fact in this stack is chosen: in
    config, resolved by hydra, with no Python-literal alternative.
    """

    model_config: ConfigDict = ConfigDict(frozen=True)

    @Flow.call
    def __call__(self, context: RenderContext) -> RenderedCore:
        raise NotImplementedError("a CoreGenerator subclass must render its core")

    def render(self) -> RenderedCore:
        """Render without constructing a context by hand.

        The context carries nothing -- a generator's inputs are all
        configuration -- so callers should not have to invent one.
        """
        return self(RenderContext(value=""))

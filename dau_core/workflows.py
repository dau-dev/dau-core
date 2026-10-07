"""Registrable ccflow workflows over the temporal goldens.

``HeroWorkflow`` is the composable form of
:func:`dau_core.hero.run_hero_workflow`: the bar clock, rolling window,
tolerance, and rolled field are configuration; the market stream under
evaluation is the runtime input, a hashable
:class:`dau_core.temporal.MarketStream` carried in a ``GenericContext``.
The result is the typed :class:`dau_core.hero.HeroResult`.
"""

# NOTE: no `from __future__ import annotations` — ccflow's Flow.call
# inspects the real annotation objects on __call__
from typing import ClassVar

from ccflow import CallableModel, Flow, GenericContext

from .hero import HeroResult, run_hero_workflow
from .temporal import MarketStream

__all__ = ("HeroWorkflow",)


class HeroWorkflow(CallableModel):
    """The predeclared align -> derive -> bar -> roll pipeline over the
    temporal goldens, recording the cardinality funnel and stage outputs."""

    model_config: ClassVar = {"frozen": True}

    bar_width: int
    roll_window: int
    tolerance: int | None = None
    roll_value: str = "close"

    def model_post_init(self, _context: object) -> None:
        if self.bar_width <= 0:
            raise ValueError(f"bar_width must be positive, got {self.bar_width}")
        if self.roll_window <= 0:
            raise ValueError(f"roll_window must be positive, got {self.roll_window}")
        if self.tolerance is not None and self.tolerance < 0:
            raise ValueError(f"tolerance must be non-negative, got {self.tolerance}")

    @Flow.call
    def __call__(self, context: GenericContext[MarketStream]) -> HeroResult:
        stream = context.value
        return run_hero_workflow(
            list(stream.trades),
            list(stream.quotes),
            bar_width=self.bar_width,
            roll_window=self.roll_window,
            tolerance=self.tolerance,
            roll_value=self.roll_value,
        )

"""Every python block in the README runs as written, each on its own.

A README that shows code that does not run is a claim the repository cannot
back. Blocks are independent (each carries its imports), so each runs in a
fresh namespace and a block that leans on an earlier one fails here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parents[2] / "README.md"
BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)
BLOCKS = BLOCK.findall(README.read_text(encoding="utf-8"))


@pytest.mark.parametrize("index", range(len(BLOCKS)), ids=[f"block-{i + 1}" for i in range(len(BLOCKS))])
def test_the_readme_block_runs_on_its_own(index: int) -> None:
    block = BLOCKS[index]
    try:
        exec(compile(block, f"README.md block {index + 1}", "exec"), {})  # noqa: S102 (the README is ours)
    except Exception as error:  # pragma: no cover - the message is the point
        raise AssertionError(f"README python block {index + 1} does not run as written: {error!r}\n{block}") from error

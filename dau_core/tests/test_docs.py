"""Every python block on the documentation pages runs as written, in order.

A page is one session: its blocks share a namespace, the way a reader typing
them into one prompt would. Where a page shows what a block prints (an
untagged fence directly after it), the output must match. A block marked with
an ``<!-- excerpt -->`` comment is quoted from a bench and is not meant to run
on its own, so it is skipped.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs" / "src"
PAGES = sorted(DOCS.glob("*/*.md"))
FENCE = re.compile(r"(<!-- excerpt -->\s*)?```(\w*)\n(.*?)```", re.DOTALL)


def _runs(page: Path) -> list[tuple[str, str | None]]:
    """The page's runnable python blocks, each with the output shown after it."""
    fences = [(bool(m.group(1)), m.group(2), m.group(3)) for m in FENCE.finditer(page.read_text(encoding="utf-8"))]
    runs = []
    for index, (excerpt, language, body) in enumerate(fences):
        if language != "python" or excerpt:
            continue
        following = fences[index + 1] if index + 1 < len(fences) else None
        shown = following[2] if following is not None and following[1] == "" else None
        runs.append((body, shown))
    return runs


def test_the_docs_pages_are_found() -> None:
    assert len(PAGES) >= 10, PAGES


@pytest.mark.parametrize("page", PAGES, ids=[f"{page.parent.name}/{page.stem}" for page in PAGES])
def test_the_page_runs_as_written(page: Path) -> None:
    namespace: dict = {}
    for number, (body, shown) in enumerate(_runs(page), start=1):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(compile(body, f"{page.name} block {number}", "exec"), namespace)  # noqa: S102 (the docs are ours)
        if shown is not None:
            assert out.getvalue().strip() == shown.strip(), f"{page.name} block {number} printed something other than the page shows"

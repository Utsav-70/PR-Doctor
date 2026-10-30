"""Tool 5 — get_tests.

"Is this change covered?" is one of the few questions that reliably separates a real
finding from a hypothetical one. It is also the input Phase 11 uses to decide which
tests are worth actually running.

Four heuristics, each contributing independently, results merged and deduplicated. No
single one is reliable: naming conventions miss integration tests, path conventions
miss monorepo layouts, and import-based detection misses tests that exercise a symbol
through a facade. Running all four and labelling which matched lets the caller judge.
"""

from __future__ import annotations

import logging
import posixpath
import re

from agent.tools.results import TestHit, ToolResult
from agent.tools.symbols import find_references, get_symbol
from agent.tools.workspace import Workspace

logger = logging.getLogger(__name__)

TOOL = "get_tests"

# A path is "a test path" if any of these appear in it. Deliberately broad: a false
# positive costs one extra file in the results, a false negative hides coverage.
TEST_DIR_MARKERS = ("tests/", "test/", "spec/", "testing/")
TEST_FILE_PATTERN = re.compile(r"(^test_|_test\.py$|\.test\.[jt]sx?$|\.spec\.[jt]sx?$|Test\.java$)")

MAX_RESULTS = 40


def is_test_path(path: str) -> bool:
    lowered = path.lower()
    if any(marker in lowered for marker in TEST_DIR_MARKERS):
        return True
    return bool(TEST_FILE_PATTERN.search(posixpath.basename(path)))


def _candidate_test_paths(source_path: str) -> list[str]:
    """Path-convention guesses for where a source file's tests would live.

    `src/payment/service.py` -> tests/payment/test_service.py, tests/test_service.py,
    src/payment/tests/test_service.py, and the same set without a `src/` prefix.
    """
    directory, _, filename = source_path.rpartition("/")
    stem = filename[:-3] if filename.endswith(".py") else filename
    trimmed = directory[4:] if directory.startswith("src/") else directory

    guesses = [
        f"tests/{trimmed}/test_{stem}.py" if trimmed else f"tests/test_{stem}.py",
        f"tests/test_{stem}.py",
        f"test/{trimmed}/test_{stem}.py" if trimmed else f"test/test_{stem}.py",
        f"{directory}/tests/test_{stem}.py" if directory else f"tests/test_{stem}.py",
        f"{directory}/test_{stem}.py" if directory else f"test_{stem}.py",
    ]
    # Stable order, no duplicates — this feeds a prompt-cache breakpoint downstream.
    return list(dict.fromkeys(guesses))


async def get_tests(
    workspace: Workspace,
    *,
    symbol: str | None = None,
    path: str | None = None,
    max_results: int = MAX_RESULTS,
) -> ToolResult:
    """Find tests related to a changed symbol or file."""
    if not symbol and not path:
        return ToolResult.failure(TOOL, "one of `symbol` or `path` is required")

    # key -> hit, so the same test found by two heuristics is reported once, labelled
    # with every heuristic that found it.
    hits: dict[tuple[str, str | None], TestHit] = {}

    def record(hit: TestHit, how: str) -> None:
        key = (hit.path, hit.name)
        existing = hits.get(key)
        if existing is None:
            hit.matched_by = how
            hits[key] = hit
        elif how not in existing.matched_by:
            existing.matched_by = f"{existing.matched_by},{how}"

    # 1 — naming convention: foo() -> test_foo, TestFoo, test_foo_*
    if symbol:
        for candidate in (f"test_{symbol}", f"Test{symbol[:1].upper()}{symbol[1:]}"):
            result = get_symbol(workspace, candidate, max_results=max_results)
            for found in result.data["symbols"] if result.ok else []:
                if is_test_path(found.path):
                    record(
                        TestHit(path=found.path, name=found.qualified_name, line=found.start_line),
                        "naming",
                    )

        # test_foo_something — prefix match, which the exact lookup above misses.
        from agent.tools.search import search_code

        prefixed = await search_code(
            workspace, rf"^\s*def (test_{re.escape(symbol)}\w*)", max_results=max_results
        )
        for match in prefixed.data["matches"] if prefixed.ok else []:
            if not is_test_path(match.path):
                continue
            name = re.search(r"def (test_\w+)", match.text)
            record(
                TestHit(path=match.path, name=name.group(1) if name else None, line=match.line),
                "naming",
            )

    # 2 — path convention
    if path:
        for guess in _candidate_test_paths(path):
            if workspace.exists(guess):
                record(TestHit(path=guess), "path")

    # 3 — import-based: test files importing the changed module. The strongest signal,
    # and the one that catches integration tests naming nothing in particular.
    if path:
        module = path.removesuffix(".py").replace("/", ".")
        tail = module.rsplit(".", 1)[-1]
        from agent.tools.search import search_code

        imports = await search_code(
            workspace,
            rf"(from|import)\s+[\w.]*\b{re.escape(tail)}\b",
            max_results=max_results,
        )
        for match in imports.data["matches"] if imports.ok else []:
            if is_test_path(match.path) and match.path != path:
                record(TestHit(path=match.path, line=match.line), "import")

    # 4 — reference-based, restricted to test paths
    if symbol:
        references = await find_references(workspace, symbol, max_results=max_results)
        for ref in references.data["references"] if references.ok else []:
            if is_test_path(ref.path):
                record(TestHit(path=ref.path, name=ref.enclosing, line=ref.line), "reference")

    ordered = sorted(hits.values(), key=lambda h: (h.path, h.line or 0, h.name or ""))
    total = len(ordered)
    return ToolResult(
        tool=TOOL,
        data={"tests": ordered[:max_results], "symbol": symbol, "path": path},
        truncated=total > max_results,
        total=total,
        note=None if ordered else "no related tests found",
    )

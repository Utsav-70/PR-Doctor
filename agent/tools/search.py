"""Tool 2 — search_code, over ripgrep.

ripgrep rather than a Python walk: an order of magnitude faster on a large tree, and
it honours .gitignore for free. `--json` because parsing ripgrep's human output with a
regex is how you get silently wrong results on a line containing a colon.

The pattern comes from a model. A model can and will send `(a+)+b`, so there is both a
static reject list and a hard subprocess timeout — the timeout is the one that actually
saves you, since no static check catches every pathological regex.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from agent.tools.results import FileLine, SearchMatch, ToolResult
from agent.tools.workspace import Workspace

logger = logging.getLogger(__name__)

TOOL = "search_code"

RIPGREP_TIMEOUT_SECONDS = 15.0
# Per-file cap, so one hot pattern cannot consume the whole result budget.
MAX_MATCHES_PER_FILE = 3
CONTEXT_LINES = 2
MAX_PATTERN_LENGTH = 500

# Nested quantifiers are the classic catastrophic-backtracking shape. This is a filter,
# not a proof — the timeout below is the real guarantee.
_DANGEROUS = re.compile(r"\([^)]*[+*]\)[+*]|\[[^\]]*\][+*]\{\d{3,}")


def _reject_pattern(pattern: str) -> str | None:
    if not pattern.strip():
        return "empty pattern"
    if len(pattern) > MAX_PATTERN_LENGTH:
        return f"pattern longer than {MAX_PATTERN_LENGTH} characters"
    if _DANGEROUS.search(pattern):
        return "pattern has nested quantifiers and risks catastrophic backtracking"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"invalid regex: {exc}"
    return None


async def search_code(
    workspace: Workspace,
    pattern: str,
    *,
    path_glob: str | None = None,
    max_results: int = 50,
) -> ToolResult:
    """Search the tree, returning matches with two lines of context either side."""
    rejection = _reject_pattern(pattern)
    if rejection:
        return ToolResult.failure(TOOL, rejection)

    args = [
        "rg",
        "--json",
        "--line-number",
        "--max-count",
        str(MAX_MATCHES_PER_FILE),
        "--context",
        str(CONTEXT_LINES),
        "--no-messages",
    ]
    if path_glob:
        args += ["--glob", path_glob]
    args += ["-e", pattern, str(workspace.root)]

    if shutil.which("rg") is None:
        # Degrade rather than fail. ripgrep is the right tool and the deployment should
        # have it (see Dockerfile), but a missing binary should not take out the whole
        # code-intelligence layer on someone's laptop.
        logger.warning("ripgrep not found; falling back to the Python scanner")
        return _search_in_python(workspace, pattern, path_glob=path_glob, max_results=max_results)

    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, _ = await asyncio.wait_for(process.communicate(), timeout=RIPGREP_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        return ToolResult.failure(TOOL, f"search timed out after {RIPGREP_TIMEOUT_SECONDS}s")
    except FileNotFoundError:
        return ToolResult.failure(TOOL, "ripgrep (rg) is not installed")

    # rg exits 1 for "no matches", which is a valid answer rather than an error.
    if process.returncode not in (0, 1):
        return ToolResult.failure(TOOL, f"ripgrep exited {process.returncode}")

    matches, total = _parse_ripgrep_json(out, workspace, max_results)
    return ToolResult(
        tool=TOOL,
        data={"matches": matches, "pattern": pattern},
        truncated=total > len(matches),
        total=total,
        note=(
            f"showing {len(matches)} of {total} matches; at most "
            f"{MAX_MATCHES_PER_FILE} per file"
            if total > len(matches)
            else None
        ),
    )


def _parse_ripgrep_json(
    raw: bytes, workspace: Workspace, max_results: int
) -> tuple[list[SearchMatch], int]:
    """Turn ripgrep's JSON-lines stream into matches with their context attached.

    ripgrep emits context lines as separate `context` events either side of each
    `match`, so they are buffered and stitched rather than arriving together.
    """
    matches: list[SearchMatch] = []
    total = 0
    pending_before: list[FileLine] = []

    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        kind = event.get("type")
        payload = event.get("data", {})

        if kind == "begin":
            pending_before = []
            continue

        text = (payload.get("lines") or {}).get("text", "").rstrip("\n")
        number = payload.get("line_number")

        if kind == "context":
            if number is None:
                continue
            entry = FileLine(line=number, text=text)
            if matches and matches[-1].path == _rel(payload, workspace) and len(matches[-1].after) < CONTEXT_LINES:
                matches[-1].after.append(entry)
            else:
                pending_before.append(entry)
                pending_before[:] = pending_before[-CONTEXT_LINES:]
            continue

        if kind == "match":
            total += 1
            if len(matches) >= max_results:
                continue
            matches.append(
                SearchMatch(
                    path=_rel(payload, workspace),
                    line=number or 0,
                    text=text,
                    before=list(pending_before),
                )
            )
            pending_before = []

    return matches, total


def _rel(payload: dict[str, Any], workspace: Workspace) -> str:
    absolute = (payload.get("path") or {}).get("text", "")
    try:
        return workspace.relative(Path(absolute))
    except ValueError:
        return absolute


# Directories never worth walking. ripgrep gets this from .gitignore; the fallback has
# to be told.
_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"})


def _search_in_python(
    workspace: Workspace,
    pattern: str,
    *,
    path_glob: str | None,
    max_results: int,
) -> ToolResult:
    """Fallback scanner, same contract as the ripgrep path.

    Slower by roughly an order of magnitude and blind to .gitignore, so it is a
    degradation rather than an equivalent. Kept deliberately simple: the correct fix
    for a large repository is to install ripgrep, not to optimise this.
    """
    regex = re.compile(pattern)
    matches: list[SearchMatch] = []
    total = 0

    for file in sorted(workspace.root.rglob(path_glob or "*")):
        if not file.is_file():
            continue
        if any(part in _SKIP_DIRS for part in file.relative_to(workspace.root).parts):
            continue
        try:
            raw = file.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue
        lines = raw.decode("utf-8", "replace").split("\n")

        in_file = 0
        for index, text in enumerate(lines):
            if not regex.search(text):
                continue
            in_file += 1
            if in_file > MAX_MATCHES_PER_FILE:
                break
            total += 1
            if len(matches) >= max_results:
                continue
            matches.append(
                SearchMatch(
                    path=workspace.relative(file),
                    line=index + 1,
                    text=text.rstrip("\n"),
                    before=[
                        FileLine(line=n + 1, text=lines[n])
                        for n in range(max(index - CONTEXT_LINES, 0), index)
                    ],
                    after=[
                        FileLine(line=n + 1, text=lines[n])
                        for n in range(index + 1, min(index + 1 + CONTEXT_LINES, len(lines)))
                    ],
                )
            )

    return ToolResult(
        tool=TOOL,
        data={"matches": matches, "pattern": pattern},
        truncated=total > len(matches),
        total=total,
        note="ripgrep unavailable; used the slower Python scanner (ignores .gitignore)",
    )

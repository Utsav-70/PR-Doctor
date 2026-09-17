"""Phase 4 — the tool-driven context builder.

Assembles the bundle every reviewer agent shares, by asking the code-intelligence
tools for what the diff implies it needs rather than dumping whole files.

Four sections, in descending priority:

    1. diff hunks            the review subject; never trimmed
    2. definitions           get_symbol on identifiers the hunks touch
    3. references            find_references on symbols the diff defines
    4. tests                 get_tests per changed file and symbol

Trimming happens from the bottom. Losing the tests section degrades a review; losing
the diff makes it meaningless — so the order is a correctness property, not a
preference.

Rendering is deterministic: sorted paths, sorted symbol names, no timestamps, no IDs.
This block is a prompt-cache breakpoint shared by all three Phase 6 agents, and a
renderer whose output varies between identical reviews silently costs ~70% more.
"""

from __future__ import annotations

import builtins
import keyword
import logging
import re
from dataclasses import dataclass, field

from agent.context.render import ContextBundle, neutralise
from agent.tools.gateway import ToolGateway
from agent.tools.results import Reference, Symbol, TestHit
from domain import PullRequestContext, PullRequestFile

logger = logging.getLogger(__name__)

# Identifiers worth asking about. Keywords and builtins are noise — nobody needs the
# definition of `len`.
_IDENTIFIER = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\b")
_UNINTERESTING = frozenset(keyword.kwlist) | set(dir(builtins)) | {
    "self", "cls", "args", "kwargs", "None", "True", "False",
}

# Definitions introduced or changed by the diff itself. These are the symbols whose
# *callers* matter: if a signature changed, every call site is a potential break.
_DEFINITION = re.compile(r"^\+\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")

# Git puts the enclosing definition after the closing `@@` of a hunk header. That is
# the only cheap way to know which function a change *inside* a body belongs to — and
# an edit inside a body is the common case, far more common than adding a new `def`.
_HUNK_ENCLOSING = re.compile(r"^@@[^@]*@@\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)")

# Per-file caps. The gateway allows 60 tool calls per review; without these a single
# 200-line diff would spend the whole budget on one file.
MAX_SYMBOLS_PER_FILE = 4
MAX_REFERENCE_SYMBOLS_PER_FILE = 2
MAX_REFERENCES_PER_SYMBOL = 8


@dataclass
class BuildReport:
    """What the builder did, and what it could not afford.

    Trimming that is not recorded reads as "there was nothing more to say", which is
    the same failure mode as a partial review presenting itself as complete.
    """

    files: int = 0
    definitions: int = 0
    references: int = 0
    tests: int = 0
    trimmed_sections: list[str] = field(default_factory=list)
    tool_calls: int = 0
    denied_calls: int = 0


def _candidate_symbols(file: PullRequestFile) -> tuple[list[str], list[str]]:
    """(identifiers worth defining, symbols this diff defines) from a patch.

    Added lines only. A symbol appearing solely in a context line was not touched by
    this PR, and the reviewer has no business commenting on it.
    """
    if not file.patch:
        return [], []

    defined: list[str] = []
    mentioned: dict[str, int] = {}

    for line in file.patch.split("\n"):
        if line.startswith("@@"):
            enclosing = _HUNK_ENCLOSING.match(line)
            if enclosing:
                defined.append(enclosing.group(1))
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        match = _DEFINITION.match(line)
        if match:
            defined.append(match.group(1))
        for name in _IDENTIFIER.findall(line[1:]):
            if name in _UNINTERESTING:
                continue
            mentioned[name] = mentioned.get(name, 0) + 1

    defined = list(dict.fromkeys(defined))

    # Symbols the diff defines come first — they are the change. Then whatever the new
    # code leans on most, by frequency, with name as a tiebreak so the order is stable.
    ranked = sorted(mentioned.items(), key=lambda kv: (-kv[1], kv[0]))
    ordered = list(dict.fromkeys(defined + [name for name, _ in ranked]))
    return ordered[:MAX_SYMBOLS_PER_FILE], defined[:MAX_REFERENCE_SYMBOLS_PER_FILE]


async def build_tool_context(
    *,
    gateway: ToolGateway,
    pr: PullRequestContext,
    agent: str = "reviewer",
    max_chars: int = 240_000,
) -> tuple[ContextBundle, BuildReport]:
    """Gather definitions, references and tests for the reviewed files."""
    report = BuildReport()
    files = sorted(pr.reviewed_files, key=lambda f: f.path)
    report.files = len(files)

    definitions: dict[str, list[Symbol]] = {}
    references: dict[str, list[Reference]] = {}
    tests: dict[str, list[TestHit]] = {}

    for file in files:
        wanted, defined = _candidate_symbols(file)

        for name in wanted:
            result = await gateway.call(agent, "get_symbol", {"name": name})
            if result.ok and result.data["symbols"]:
                definitions.setdefault(name, []).extend(result.data["symbols"])

        for name in defined:
            result = await gateway.call(
                agent, "find_references", {"name": name, "max_results": MAX_REFERENCES_PER_SYMBOL}
            )
            if result.ok and result.data["references"]:
                references.setdefault(name, []).extend(result.data["references"])

        result = await gateway.call(agent, "get_tests", {"path": file.path})
        if result.ok and result.data["tests"]:
            tests.setdefault(file.path, []).extend(result.data["tests"])

    report.definitions = sum(len(v) for v in definitions.values())
    report.references = sum(len(v) for v in references.values())
    report.tests = sum(len(v) for v in tests.values())
    report.tool_calls = gateway.usage.calls
    report.denied_calls = gateway.usage.denied

    sections = [
        ("definitions", _render_definitions(definitions)),
        ("references", _render_references(references)),
        ("tests", _render_tests(tests)),
    ]
    diff_block = _render_diff(pr, files)

    # The diff is not in the trim list: it is the thing being reviewed.
    budget = max_chars - len(diff_block)
    kept: list[str] = []
    for name, rendered in sections:
        if not rendered:
            continue
        if len(rendered) > budget:
            report.trimmed_sections.append(name)
            continue
        kept.append(rendered)
        budget -= len(rendered)

    if report.trimmed_sections:
        logger.info("context trimmed", extra={"sections": report.trimmed_sections})

    repository_block = "\n".join(
        ['<repository_content untrusted="true">', *kept, "</repository_content>"]
    )
    return (
        ContextBundle(
            repository_block=repository_block,
            diff_block=diff_block,
            included_paths=[f.path for f in files],
            skipped_paths=[f.path for f in pr.skipped_files],
            truncated=bool(report.trimmed_sections),
        ),
        report,
    )


def _render_diff(pr: PullRequestContext, files: list[PullRequestFile]) -> str:
    parts = [
        "<pull_request>",
        f"<title>{neutralise(pr.title)}</title>",
        f"<description>\n{neutralise(pr.description[:4000])}\n</description>",
        "<diff>",
    ]
    for file in files:
        if not file.patch:
            continue
        parts.append(
            f'<file_diff path="{neutralise(file.path)}" '
            f'language="{file.language}" change="{file.change_type}">'
        )
        parts.append(neutralise(file.patch))
        parts.append("</file_diff>")
    parts.extend(["</diff>", "</pull_request>"])
    return "\n".join(parts)


def _render_definitions(definitions: dict[str, list[Symbol]]) -> str:
    if not definitions:
        return ""
    parts = ["<definitions>"]
    for name in sorted(definitions):
        # Dedupe across files, then sort — two files can report the same import.
        seen = {(s.path, s.start_line, s.qualified_name): s for s in definitions[name]}
        for key in sorted(seen):
            symbol = seen[key]
            parts.append(
                f'<symbol name="{neutralise(symbol.qualified_name)}" kind="{symbol.kind}" '
                f'path="{neutralise(symbol.path)}" lines="{symbol.start_line}-{symbol.end_line}"'
                + (' partial_parse="true"' if symbol.partial_parse else "")
                + ">"
            )
            parts.append(neutralise(symbol.signature))
            if symbol.docstring:
                parts.append(f"<doc>{neutralise(symbol.docstring[:400])}</doc>")
            if symbol.body:
                parts.append(f"<body>\n{neutralise(symbol.body)}\n</body>")
            parts.append("</symbol>")
    parts.append("</definitions>")
    return "\n".join(parts)


def _render_references(references: dict[str, list[Reference]]) -> str:
    if not references:
        return ""
    # The heuristic label travels with the data. Phase 7 weights evidence by it, and a
    # reference list that looks authoritative gets findings confirmed that should have
    # been doubted.
    parts = ['<references confidence="heuristic" note="no type inference or dynamic dispatch">']
    for name in sorted(references):
        seen = {(r.path, r.line): r for r in references[name]}
        parts.append(f'<symbol name="{neutralise(name)}" count="{len(seen)}">')
        for key in sorted(seen):
            ref = seen[key]
            parts.append(
                f'  <use path="{neutralise(ref.path)}" line="{ref.line}" kind="{ref.kind}"'
                f' in="{neutralise(ref.enclosing or "")}">{neutralise(ref.text[:160])}</use>'
            )
        parts.append("</symbol>")
    parts.append("</references>")
    return "\n".join(parts)


def _render_tests(tests: dict[str, list[TestHit]]) -> str:
    if not tests:
        return ""
    parts = ["<related_tests>"]
    for path in sorted(tests):
        seen = {(t.path, t.name, t.line): t for t in tests[path]}
        parts.append(f'<for path="{neutralise(path)}">')
        for key in sorted(seen, key=lambda k: (k[0], k[2] or 0, k[1] or "")):
            hit = seen[key]
            parts.append(
                f'  <test path="{neutralise(hit.path)}"'
                + (f' name="{neutralise(hit.name)}"' if hit.name else "")
                + (f' line="{hit.line}"' if hit.line else "")
                + f' matched_by="{hit.matched_by}"/>'
            )
        parts.append("</for>")
    parts.append("</related_tests>")
    return "\n".join(parts)

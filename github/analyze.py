"""Phase 3 — raw GitHub data to an analysed PullRequestContext.

Deterministic, no LLM, no network, no database. Everything here is a pure function of
the `/pulls/{n}/files` payload, which is what lets the whole analysis stage be replayed
from a saved fixture.

Three things happen, in order: parse each patch, classify each file, then spend the
line budget on whatever survived.
"""

from __future__ import annotations

import logging
from typing import Any

from domain import PullRequestContext, PullRequestFile
from github.classify import classify
from github.diff import parse_files

logger = logging.getLogger(__name__)


def analyze_files(
    files_payload: list[dict[str, Any]],
    *,
    ignore_globs: list[str] | None = None,
) -> list[PullRequestFile]:
    """Parse and classify, without applying any budget."""
    analysed: list[PullRequestFile] = []
    for diff in parse_files(files_payload):
        verdict = classify(
            diff.path,
            has_patch=bool(diff.patch),
            change_type=diff.change_type,
            ignore_globs=ignore_globs,
        )
        analysed.append(
            PullRequestFile(
                path=diff.path,
                previous_path=diff.previous_path,
                change_type=diff.change_type,
                language=verdict.language,
                category=verdict.category,
                additions=diff.additions,
                deletions=diff.deletions,
                patch=diff.patch,
                diff_line_map=diff.line_map,
                added_line_numbers=diff.added_lines,
                reviewed=verdict.reviewed,
                skip_reason=verdict.skip_reason,
            )
        )
    return analysed


def apply_budget(files: list[PullRequestFile], max_changed_lines: int) -> tuple[bool, str | None]:
    """Spend the changed-line budget, smallest files first.

    Smallest-first maximises files reviewed per token. Files that do not fit are
    flipped to `reviewed=False` with `skip_reason="budget"` rather than dropped from
    the list — a review that silently examined a quarter of the diff and reported
    itself complete is a correctness bug, so the omission has to be visible.

    Mutates `files` in place and returns (is_partial, partial_reason).
    """
    candidates = [f for f in files if f.reviewed]
    total = sum(f.changed_lines for f in candidates)
    if total <= max_changed_lines:
        return False, None

    spent = 0
    for file in sorted(candidates, key=lambda f: f.changed_lines):
        if spent + file.changed_lines > max_changed_lines:
            file.reviewed = False
            file.skip_reason = "budget"
            continue
        spent += file.changed_lines

    dropped = sum(1 for f in files if f.skip_reason == "budget")
    logger.info(
        "budget exceeded",
        extra={"changed_lines": total, "budget": max_changed_lines, "files_dropped": dropped},
    )
    return True, "diff_too_large"


def build_pull_request_context(
    *,
    pr: dict[str, Any],
    files_payload: list[dict[str, Any]],
    repository_full_name: str,
    ignore_globs: list[str] | None = None,
    max_changed_lines: int,
) -> PullRequestContext:
    """The Phase 3 entry point: everything the reviewer needs, from one API payload."""
    files = analyze_files(files_payload, ignore_globs=ignore_globs)
    is_partial, partial_reason = apply_budget(files, max_changed_lines)

    return PullRequestContext(
        repository_full_name=repository_full_name,
        pr_number=int(pr["number"]),
        head_sha=str(pr["head"]["sha"]),
        base_sha=str(pr["base"]["sha"]),
        title=str(pr.get("title") or ""),
        description=str(pr.get("body") or ""),
        files=files,
        is_partial=is_partial,
        partial_reason=partial_reason,
    )

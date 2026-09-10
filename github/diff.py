"""Unified-diff parsing.

The important output is the line map: new-file line number -> GitHub diff position.
GitHub anchors review comments to a *position within the diff*, not a file line, so
this mapping is what makes a comment land on the right line later. It is also the
easiest thing in the project to get subtly wrong, which is why it lives here as a pure
function over a patch string.

GitHub's rule: position 0 is the first `@@` hunk header; the line after it is
position 1; the count continues through blank lines and subsequent hunk headers until
the end of the file's patch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@")


@dataclass(slots=True)
class FileDiff:
    """One changed file, parsed."""

    path: str
    change_type: str
    additions: int
    deletions: int
    patch: str | None
    previous_path: str | None = None
    # new-file line number -> diff position
    line_map: dict[int, int] = field(default_factory=dict)
    # new-file line numbers that this PR added or modified
    added_lines: set[int] = field(default_factory=set)

    @property
    def is_reviewable(self) -> bool:
        return bool(self.patch) and self.change_type != "removed"


def build_line_map(patch: str) -> tuple[dict[int, int], set[int]]:
    """Return (line_map, added_line_numbers) for a single file's patch.

    `line_map` covers added *and* context lines, because a finding may legitimately
    cite a context line inside a hunk. `added_line_numbers` is the stricter set used
    to decide whether a finding is about code this PR actually changed.
    """
    line_map: dict[int, int] = {}
    added: set[int] = set()
    position = 0
    new_line = 0
    seen_hunk = False

    # A trailing newline would split into a final empty string, which then reads as
    # a context line and invents a mapping for a line that does not exist.
    for raw in patch.rstrip("\n").split("\n"):
        header = HUNK_HEADER.match(raw)
        if header:
            new_line = int(header.group("new_start"))
            if seen_hunk:
                # A subsequent hunk header occupies a position of its own.
                position += 1
            else:
                seen_hunk = True
                position = 0
            continue

        if not seen_hunk:
            continue  # patch preamble (---/+++ lines)

        position += 1

        if raw.startswith("+"):
            line_map[new_line] = position
            added.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            pass  # removed line: exists only in the old file
        elif raw.startswith("\\"):
            pass  # "\ No newline at end of file": occupies a position, is not a line
        else:
            line_map[new_line] = position
            new_line += 1

    return line_map, added


def parse_files(files_payload: list[dict[str, Any]]) -> list[FileDiff]:
    """Turn the `/pulls/{n}/files` response into parsed FileDiffs.

    GitHub omits `patch` for binary files and for very large ones. Those come back
    with an empty line map and are excluded from review rather than being silently
    treated as reviewed.
    """
    result: list[FileDiff] = []
    for entry in files_payload:
        patch = entry.get("patch")
        patch_str = str(patch) if isinstance(patch, str) else None

        diff = FileDiff(
            path=str(entry.get("filename", "")),
            change_type=str(entry.get("status", "modified")),
            additions=int(entry.get("additions", 0) or 0),
            deletions=int(entry.get("deletions", 0) or 0),
            patch=patch_str,
            previous_path=(
                str(entry["previous_filename"])
                if isinstance(entry.get("previous_filename"), str)
                else None
            ),
        )
        if patch_str:
            diff.line_map, diff.added_lines = build_line_map(patch_str)
        result.append(diff)
    return result

"""Tool 1 — read_file.

The highest-risk tool in the project: its path argument is model-supplied, and the
model has been reading code written by whoever opened the PR. Containment is enforced
by Workspace.resolve, which is the only thing between this and `.env`.
"""

from __future__ import annotations

from agent.tools.results import FileLine, ToolResult
from agent.tools.workspace import PathNotAllowed, Workspace

TOOL = "read_file"

# Sniffed for NUL to decide "binary". 8 KiB is what git uses.
_BINARY_SNIFF_BYTES = 8192
# When a file exceeds the byte cap, show this many lines from each end. Head-and-tail
# beats head-only: the imports and the trailing definitions are both informative.
_HEAD_TAIL_LINES = 80


def read_file(
    workspace: Workspace,
    path: str,
    *,
    start_line: int | None = None,
    end_line: int | None = None,
    max_bytes: int = 200_000,
) -> ToolResult:
    """Read a file, or a line range, with line numbers attached.

    Numbers are attached so the agent can cite a line and the citation can be checked
    against the diff later. A citation that cannot be validated is indistinguishable
    from one that is wrong.
    """
    try:
        target = workspace.resolve(path)
    except PathNotAllowed as exc:
        return ToolResult.failure(TOOL, str(exc))

    if not target.exists():
        return ToolResult.failure(TOOL, f"file not found: {path}")
    if not target.is_file():
        return ToolResult.failure(TOOL, f"not a regular file: {path}")

    raw = target.read_bytes()
    if b"\x00" in raw[:_BINARY_SNIFF_BYTES]:
        return ToolResult(
            tool=TOOL,
            data={"path": workspace.relative(target), "binary": True, "bytes": len(raw)},
            note="binary file; not decoded",
        )

    truncated = False
    note: str | None = None
    if len(raw) > max_bytes:
        truncated = True

    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total_lines = len(lines)

    if start_line is not None or end_line is not None:
        first = max(start_line or 1, 1)
        last = min(end_line or total_lines, total_lines)
        if first > total_lines:
            return ToolResult.failure(TOOL, f"start_line {first} is past end of file ({total_lines})")
        selected = [FileLine(line=n, text=lines[n - 1]) for n in range(first, last + 1)]
        return ToolResult(
            tool=TOOL,
            data={"path": workspace.relative(target), "lines": selected, "total_lines": total_lines},
            truncated=last < total_lines or first > 1,
            total=total_lines,
        )

    if truncated:
        # Never silently drop the middle — say exactly which lines are missing.
        head = [FileLine(line=n, text=lines[n - 1]) for n in range(1, min(_HEAD_TAIL_LINES, total_lines) + 1)]
        tail_start = max(total_lines - _HEAD_TAIL_LINES + 1, len(head) + 1)
        tail = [FileLine(line=n, text=lines[n - 1]) for n in range(tail_start, total_lines + 1)]
        note = f"file is {len(raw)} bytes; omitted lines {len(head) + 1}-{tail_start - 1}"
        return ToolResult(
            tool=TOOL,
            data={"path": workspace.relative(target), "lines": head + tail, "total_lines": total_lines},
            truncated=True,
            total=total_lines,
            note=note,
        )

    return ToolResult(
        tool=TOOL,
        data={
            "path": workspace.relative(target),
            "lines": [FileLine(line=n, text=t) for n, t in enumerate(lines, start=1)],
            "total_lines": total_lines,
        },
        total=total_lines,
    )

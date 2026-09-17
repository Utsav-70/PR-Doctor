"""Context assembly.

Builds the two blocks the reviewer sees: repository content (cacheable) and the diff
(not cacheable). Everything here is repository content authored by whoever opened the
PR, so it is rendered as clearly-delimited, line-numbered *data* — never as prose that
could read as instruction.

This module renders; it does not decide. Which files are worth reviewing is settled
upstream in github/analyze.py and arrives here as `PullRequestFile.reviewed`. Keeping
policy out of the renderer is what lets `review_files.skip_reason` be a complete record
of why anything was left out.
"""

from __future__ import annotations

from dataclasses import dataclass

from domain import PullRequestFile

# Framing that repository content must not be able to forge.
DELIMITER_PATTERNS = (
    "</repository_content>",
    "<repository_content",
    "</file>",
    "<system>",
    "<system-reminder>",
    "\nHuman:",
    "\nAssistant:",
)


@dataclass(slots=True)
class ContextBundle:
    repository_block: str
    diff_block: str
    included_paths: list[str]
    skipped_paths: list[str]
    truncated: bool = False


def neutralise(text: str) -> str:
    """Strip framing that repository content could use to escape its delimiters.

    Injection is not *solved* by this — it is one layer. The structural guarantee is
    that repository content is always rendered inside an explicitly untrusted block.
    """
    cleaned = text
    for pattern in DELIMITER_PATTERNS:
        cleaned = cleaned.replace(pattern, pattern.replace("<", "&lt;").replace("\n", " "))
    return cleaned


def render_numbered(content: str, *, start: int = 1) -> str:
    """Line-number a file so the model can cite a line and we can verify the citation."""
    lines = content.split("\n")
    width = len(str(start + len(lines) - 1))
    return "\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(lines, start=start))


def build_context(
    *,
    title: str,
    description: str,
    files: list[PullRequestFile],
    file_contents: dict[str, str],
    max_chars: int,
) -> ContextBundle:
    """Assemble the repository and diff blocks.

    Rendering is deterministic — files in a stable order, no timestamps, no IDs — so
    the repository block is byte-identical across reviews of the same commit and can
    sit behind a prompt-cache breakpoint.
    """
    included: list[str] = []
    skipped: list[str] = []
    truncated = False

    parts: list[str] = ['<repository_content untrusted="true">']
    budget = max_chars

    for file in sorted(files, key=lambda f: f.path):
        content = file_contents.get(file.path)
        if content is None:
            skipped.append(file.path)
            continue

        rendered = f'<file path="{neutralise(file.path)}">\n{render_numbered(neutralise(content))}\n</file>'
        if len(rendered) > budget:
            truncated = True
            skipped.append(file.path)
            continue

        parts.append(rendered)
        budget -= len(rendered)
        included.append(file.path)

    parts.append("</repository_content>")

    diff_parts: list[str] = [
        "<pull_request>",
        f"<title>{neutralise(title)}</title>",
        f"<description>\n{neutralise(description[:4000])}\n</description>",
        "<diff>",
    ]
    for file in sorted(files, key=lambda f: f.path):
        if not file.patch:
            continue
        diff_parts.append(f'<file_diff path="{neutralise(file.path)}" change="{file.change_type}">')
        diff_parts.append(neutralise(file.patch))
        diff_parts.append("</file_diff>")
    diff_parts.extend(["</diff>", "</pull_request>"])

    return ContextBundle(
        repository_block="\n".join(parts),
        diff_block="\n".join(diff_parts),
        included_paths=included,
        skipped_paths=skipped,
        truncated=truncated,
    )

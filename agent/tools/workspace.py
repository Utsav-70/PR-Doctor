"""The checked-out tree, and the only sanctioned way to turn a path into a real one.

Every tool argument that names a file passes through `Workspace.resolve`. Those
arguments come from a model that has been reading attacker-influenced code, so this is
the highest-risk surface in the project: a successful traversal reads anything the
worker process can read, including `.env`.

Two rules, both load-bearing:

- Containment is checked with `Path.resolve()` and `is_relative_to()`, never with
  `startswith`. `/repo/../repo-evil/x` has the right prefix and the wrong target.
- The root is resolved once at construction. On macOS `/tmp` is a symlink to
  `/private/tmp`, so an unresolved root makes every containment check fail — which is
  the failure mode that tempts people to "just use startswith".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Files that exist inside a repository and must never be readable through a tool, even
# though they are legitimately inside the root. The clone strips credentials from
# .git/config, but defence in depth is cheap here.
DENIED_NAMES: frozenset[str] = frozenset({".env", ".env.local", ".npmrc", ".netrc"})
DENIED_DIRS: frozenset[str] = frozenset({".git"})


class PathNotAllowed(ValueError):
    """Raised when a tool argument resolves outside the workspace, or onto a denied path.

    A ValueError rather than a bespoke hierarchy because the gateway turns it into a
    typed error result — the agent should see "that path is not allowed" and try
    something else, not kill the review.
    """


@dataclass(frozen=True, slots=True)
class Workspace:
    """A read-only checkout at one commit."""

    root: Path
    repository_full_name: str = ""
    head_sha: str = ""

    @classmethod
    def at(cls, root: Path | str, **kwargs: str) -> Workspace:
        resolved = Path(root).resolve(strict=True)
        if not resolved.is_dir():
            raise PathNotAllowed(f"workspace root is not a directory: {root}")
        return cls(root=resolved, **kwargs)

    def resolve(self, relative: str) -> Path:
        """Turn a model-supplied path into a real one inside this workspace.

        Raises PathNotAllowed for absolute paths, traversal, symlink escapes, and the
        deny list. Does not require the file to exist — a missing file is the caller's
        "not found", not a security failure.
        """
        if not relative or relative.strip() == "":
            raise PathNotAllowed("empty path")

        candidate = Path(relative)
        if candidate.is_absolute():
            raise PathNotAllowed(f"absolute paths are not allowed: {relative}")
        if candidate.drive or candidate.root:
            raise PathNotAllowed(f"rooted paths are not allowed: {relative}")

        # strict=False: the path need not exist, but any symlink along the way that
        # does exist is followed, which is what catches an escaping link.
        target = (self.root / candidate).resolve(strict=False)

        if not target.is_relative_to(self.root):
            raise PathNotAllowed(f"path escapes the workspace: {relative}")

        rel_parts = target.relative_to(self.root).parts
        if any(part in DENIED_DIRS for part in rel_parts):
            raise PathNotAllowed(f"path is in a denied directory: {relative}")
        if rel_parts and rel_parts[-1] in DENIED_NAMES:
            raise PathNotAllowed(f"path is denied: {relative}")

        return target

    def relative(self, target: Path) -> str:
        """Path as the agent should see it — relative, forward slashes, no root leak."""
        return target.resolve(strict=False).relative_to(self.root).as_posix()

    def exists(self, relative: str) -> bool:
        try:
            return self.resolve(relative).is_file()
        except PathNotAllowed:
            return False

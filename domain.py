"""Shared data shapes.

The boundary object between fetching and reviewing. `github/` produces a
`PullRequestContext`; `agent/` consumes one. Neither imports the other, which is what
01-architecture.md asks for and what makes a future GitLab adapter a matter of writing
one producer.

Everything here is plain Pydantic — no SQLAlchemy, no network, no settings. Phase 3's
exit criterion is that this object builds "from a saved webhook payload with no DB and
no network", so importing anything stateful here would quietly break that.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from github.classify import Category, Language


class PullRequestFile(BaseModel):
    """One changed file, fetched and analysed.

    This is the analysed form. `github.diff.FileDiff` is the raw parse — it knows about
    patches and positions but nothing about what a file *is*. Keeping the two separate
    is the Phase 2 / Phase 3 split: parsing is mechanical, classification is policy.
    """

    path: str
    previous_path: str | None = None
    change_type: str = "modified"

    language: Language = "unknown"
    category: Category = "source"

    additions: int = 0
    deletions: int = 0

    # None when GitHub omitted it: binary, oversized, or a mode-only change.
    patch: str | None = None

    # new-file line number -> position within the diff. GitHub's review API anchors
    # comments to a diff position, not a file line, so this is what Phase 9 needs.
    # Computed once here rather than re-parsed at post time.
    diff_line_map: dict[int, int] = Field(default_factory=dict)

    # The stricter set: lines this PR actually added or modified. A finding about a
    # line outside this set is about code nobody touched.
    added_line_numbers: set[int] = Field(default_factory=set)

    reviewed: bool = False
    skip_reason: str | None = None

    @property
    def changed_lines(self) -> int:
        return self.additions + self.deletions

    def position_for(self, line: int) -> int | None:
        """Diff position for a new-file line, or None if the line is not in the diff."""
        return self.diff_line_map.get(line)


class PullRequestContext(BaseModel):
    """Everything the reviewer needs about a pull request, and nothing about how it
    was fetched."""

    repository_full_name: str
    pr_number: int
    head_sha: str
    base_sha: str

    title: str = ""
    description: str = ""

    files: list[PullRequestFile] = Field(default_factory=list)

    # True when the budget forced a subset. A review that examined a quarter of the
    # diff and presents itself as complete is a correctness bug, not a nuance.
    is_partial: bool = False
    partial_reason: str | None = None

    @property
    def reviewed_files(self) -> list[PullRequestFile]:
        return [f for f in self.files if f.reviewed]

    @property
    def skipped_files(self) -> list[PullRequestFile]:
        return [f for f in self.files if not f.reviewed]

    @property
    def changed_files(self) -> int:
        return len(self.files)

    @property
    def added_lines(self) -> int:
        return sum(f.additions for f in self.files)

    @property
    def deleted_lines(self) -> int:
        return sum(f.deletions for f in self.files)

    @property
    def changed_lines(self) -> int:
        """Budget metric: counted over reviewed files only.

        Deliberately not the same as added_lines + deleted_lines. A PR whose size comes
        from a lockfile should not be called too large to review.
        """
        return sum(f.changed_lines for f in self.reviewed_files)

    def file(self, path: str) -> PullRequestFile | None:
        return next((f for f in self.files if f.path == path), None)

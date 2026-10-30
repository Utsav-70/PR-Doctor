"""The shape every tool returns.

Errors are values, not exceptions. A tool that cannot find a file returns
`ToolResult(ok=False, error=...)` and the agent adapts; raising would abort a review
that had one bad path argument out of thirty good ones.

Every result that could have been larger says so. `truncated` plus `total` is the
difference between "there are three call sites" and "I showed you three call sites" —
an agent that cannot tell them apart will state the first when only the second is true.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolResult(BaseModel):
    tool: str
    ok: bool = True
    error: str | None = None

    data: Any = None

    # Set when a cap was hit. `total` is the real count when it is knowable.
    truncated: bool = False
    total: int | None = None
    note: str | None = None

    @classmethod
    def failure(cls, tool: str, error: str) -> ToolResult:
        return cls(tool=tool, ok=False, error=error)


class FileLine(BaseModel):
    line: int
    text: str


class SearchMatch(BaseModel):
    path: str
    line: int
    text: str
    before: list[FileLine] = Field(default_factory=list)
    after: list[FileLine] = Field(default_factory=list)


SymbolKind = Literal["function", "method", "class", "import", "assignment", "attribute", "call"]


class Symbol(BaseModel):
    path: str
    name: str
    qualified_name: str
    kind: SymbolKind
    start_line: int
    end_line: int
    signature: str = ""
    docstring: str | None = None
    body: str | None = None
    # Tree-sitter parses partially through syntax errors by design. A partial parse is
    # useful, but the caller has to know it was one.
    partial_parse: bool = False


class Reference(BaseModel):
    path: str
    line: int
    kind: SymbolKind
    enclosing: str | None = None
    text: str = ""
    # find_references is not a resolver: no type inference, no dynamic dispatch.
    # Phase 7 weights evidence by this, so over-claiming here corrupts that judgement.
    confidence: Literal["heuristic"] = "heuristic"


class TestHit(BaseModel):
    path: str
    name: str | None = None
    line: int | None = None
    # Which heuristic found it: naming, path, import, reference.
    matched_by: str = ""

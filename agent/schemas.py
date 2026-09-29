"""Structured output schema for the reviewer.

The API enforces this shape, so there is no JSON parsing, no regex extraction, and no
retry-on-parse-failure loop anywhere in the codebase. Field descriptions are part of
the prompt the model sees — they do real work, so keep them precise.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
Category = Literal["BUG", "SECURITY", "PERFORMANCE", "RELIABILITY", "MAINTAINABILITY"]

SEVERITY_ORDER: dict[str, int] = {
    "CRITICAL": 0,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 3,
    "INFO": 4,
}


class Finding(BaseModel):
    file: str = Field(description="Repository-relative path, exactly as it appears in the diff")
    line: int = Field(description="Line number in the NEW version of the file", ge=1)
    severity: Severity = Field(description="How much this matters if it is real")
    category: Category
    title: str = Field(description="One line stating the problem, at most 100 characters")
    description: str = Field(
        description=(
            "Two to four sentences: what is wrong, why it matters, and the conditions "
            "under which it goes wrong. This text is shown to the developer verbatim."
        )
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Your honest probability that this is a real problem. Do not inflate it; "
            "a low number is useful information, not a failure."
        ),
    )


class FindingsReport(BaseModel):
    findings: list[Finding] = Field(
        description="Every issue found. An empty list is a valid and common answer."
    )


# --- Phase 7: evidence ---------------------------------------------------------------

# Which checks exist. `semgrep`/`ruff`/`bandit` are declared now and unavailable until
# Phase 10 — the agent may plan them, the executor records them as `error` with a reason.
# Declaring them early means the plan schema does not change when Phase 10 lands.
CheckMethod = Literal["search", "ast", "dataflow", "tests", "semgrep", "ruff", "bandit"]
Outcome = Literal["supports", "refutes", "inconclusive", "error"]

# How much each method's verdict is worth. A text match proves presence, not semantics;
# an AST fact is structural. `dataflow` rides on find_references, which does no type
# inference — the weight states that honestly rather than laundering it.
EVIDENCE_WEIGHT: dict[str, float] = {
    "tests": 1.00,
    "semgrep": 0.90,
    "ast": 0.85,
    "bandit": 0.75,
    "ruff": 0.75,
    "dataflow": 0.70,
    "search": 0.60,
}


class Check(BaseModel):
    """One verification check.

    Arguments are explicit optional fields rather than an open `dict`. Two reasons, and
    the second is the one that forced it:

    1. The model can only name arguments that exist, instead of inventing keys the
       gateway then rejects at validation time.
    2. A `dict[str, Any]` renders as `additionalProperties` in JSON Schema, and the
       Gemini Developer API refuses any schema containing it.
    """

    method: CheckMethod
    rationale: str = Field(description="What this check would settle, in one sentence")
    pattern: str | None = Field(default=None, description="search: the regex to look for")
    path_glob: str | None = Field(default=None, description="search: restrict to these paths")
    name: str | None = Field(default=None, description="ast / dataflow: the symbol name")
    path: str | None = Field(default=None, description="tests: the file to find tests for")
    symbol: str | None = Field(default=None, description="tests: the symbol to find tests for")

    def tool_args(self) -> dict[str, Any]:
        """The arguments this check's tool actually takes, dropping the unset ones."""
        by_method: dict[str, dict[str, Any]] = {
            "search": {"pattern": self.pattern, "path_glob": self.path_glob},
            "ast": {"name": self.name},
            "dataflow": {"name": self.name},
            "tests": {"path": self.path, "symbol": self.symbol},
        }
        return {k: v for k, v in by_method.get(self.method, {}).items() if v is not None}


class VerificationPlan(BaseModel):
    """What would settle these claims — chosen before any tool has run."""

    checks: list[Check] = Field(
        description="At most a handful. Each must be capable of refuting, not only confirming."
    )


class EvidenceVerdict(BaseModel):
    finding_index: int = Field(
        description="Index of the finding this verdict is for, as numbered in the prompt", ge=0
    )
    verified: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    supporting: list[str] = Field(default_factory=list)
    refuting: list[str] = Field(default_factory=list)


class EvidenceVerdicts(BaseModel):
    verdicts: list[EvidenceVerdict]


# --- Phase 8: judgment ---------------------------------------------------------------

RejectionCode = Literal[
    "duplicate", "weak_evidence", "irrelevant", "false_positive", "below_threshold"
]


class Judgment(BaseModel):
    finding_index: int = Field(description="Index of the finding, as numbered in the prompt", ge=0)
    accepted: bool
    severity: Severity = Field(description="The severity you consider correct, not the reported one")
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(description="Why accepted or rejected, one or two sentences")
    rejection_code: RejectionCode | None = None
    revised_description: str | None = Field(
        default=None,
        description=(
            "Rewritten description if the original was unclear or overstated. Null to keep it."
        ),
    )
    duplicate_of: int | None = Field(
        default=None,
        description="If this duplicates another finding, the index it duplicates",
    )


class JudgeVerdict(BaseModel):
    judgments: list[Judgment]


class ReviewUsage(BaseModel):
    """Token and cost accounting for one call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str | None = None


class CallRecord(BaseModel):
    """One request to a provider, for the `llm_calls` row.

    Kept per call rather than summed: once Phase 6 fans out, "which stage cost what" is
    the question worth answering, and a total cannot be un-summed.
    """

    stage: str = "review"
    provider: str = "anthropic"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str | None = None
    duration_ms: int = 0
    tool_iterations: int = 0
    error: str | None = None


class ReviewResult(BaseModel):
    report: FindingsReport
    usage: ReviewUsage
    refused: bool = False
    # Every provider call this review made, in order. `usage` is their sum.
    calls: list[CallRecord] = Field(default_factory=list)
    # How many investigation rounds the agent used before reporting. A reviewer that
    # always uses zero is not investigating; one that always hits the cap is lost.
    tool_iterations: int = 0
    budget_stopped: str | None = None

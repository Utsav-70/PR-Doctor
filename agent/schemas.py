"""Structured output schema for the reviewer.

The API enforces this shape, so there is no JSON parsing, no regex extraction, and no
retry-on-parse-failure loop anywhere in the codebase. Field descriptions are part of
the prompt the model sees — they do real work, so keep them precise.
"""

from __future__ import annotations

from typing import Literal

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


class ReviewUsage(BaseModel):
    """Token and cost accounting for one call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0
    stop_reason: str | None = None


class ReviewResult(BaseModel):
    report: FindingsReport
    usage: ReviewUsage
    refused: bool = False

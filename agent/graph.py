"""Phase 5 — the review graph.

    START → fetch → build_context → review → validate → END

Four nodes, deliberately linear. LangGraph is not here for branching; it is here for
the typed state, for the checkpointer Phase 13 needs, and for the pipeline shape being
data rather than nested function calls. Phase 6 adds the fan-out to that shape without
restructuring the worker.

State is append-only per node, and **a node that fails appends to `errors` and returns
rather than raising**. That is the difference between a review that degrades — no
tests section, or no findings but accurate file counts — and one that is lost. The
worker only sees an exception if something outside the graph breaks.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.context import ContextBundle, build_tool_context
from agent.reviewer import review_diff
from agent.schemas import SEVERITY_ORDER, CallRecord
from agent.schemas import Finding as FindingSchema
from agent.tools.gateway import ToolGateway
from domain import PullRequestContext
from settings import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StageError:
    stage: str
    kind: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class DroppedFinding:
    finding: FindingSchema
    reason: str


@dataclass
class UsageAccumulator:
    calls: list[CallRecord] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    @property
    def input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.calls)

    @property
    def output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.calls)

    @property
    def cache_read_tokens(self) -> int:
        return sum(c.cache_read_tokens for c in self.calls)

    @property
    def cache_creation_tokens(self) -> int:
        return sum(c.cache_creation_tokens for c in self.calls)


class ReviewState(TypedDict, total=False):
    review_id: str
    pr: PullRequestContext
    # Declared, not stashed under an ad-hoc key: LangGraph propagates only the fields
    # in this schema between nodes, and silently discards anything else. An undeclared
    # hand-off key loses the data with no error at all.
    raw_findings: list[FindingSchema]
    gateway: ToolGateway | None
    context_bundle: ContextBundle | None
    findings: list[tuple[FindingSchema, int]]
    dropped: list[DroppedFinding]
    errors: list[StageError]
    usage: UsageAccumulator
    refused: bool
    budget_stopped: str | None
    tool_iterations: int


# --- nodes ---------------------------------------------------------------------------


async def fetch_node(state: ReviewState) -> ReviewState:
    """The PullRequestContext is built by the worker before the graph runs.

    Kept as a node anyway: Phase 13's checkpointer resumes at node boundaries, and a
    resume that skipped straight to an expensive LLM call on a stale context would be
    worse than one extra no-op.
    """
    pr = state["pr"]
    if not pr.reviewed_files:
        state.setdefault("errors", []).append(
            StageError("fetch", "nothing_reviewable", "no reviewable files")
        )
    return state


async def build_context_node(state: ReviewState) -> ReviewState:
    gateway = state.get("gateway")
    if gateway is None:
        state.setdefault("errors", []).append(
            StageError("build_context", "no_gateway", "tools unavailable")
        )
        return state
    try:
        bundle, report = await build_tool_context(
            gateway=gateway,
            pr=state["pr"],
            max_chars=get_settings().MAX_CONTEXT_CHARS,
        )
        state["context_bundle"] = bundle
        if report.trimmed_sections:
            state.setdefault("errors", []).append(
                StageError("build_context", "trimmed", ",".join(report.trimmed_sections))
            )
    except Exception as exc:
        logger.exception("context build failed")
        state.setdefault("errors", []).append(
            StageError("build_context", type(exc).__name__, str(exc)[:300])
        )
    return state


async def review_node(state: ReviewState) -> ReviewState:
    bundle = state.get("context_bundle")
    if bundle is None:
        state.setdefault("errors", []).append(
            StageError("review", "no_context", "context bundle missing")
        )
        return state

    try:
        result = await review_diff(
            bundle.repository_block, bundle.diff_block, gateway=state.get("gateway")
        )
    except Exception as exc:
        logger.exception("review call failed")
        state.setdefault("errors", []).append(
            StageError("review", type(exc).__name__, str(exc)[:300])
        )
        return state

    usage = state.setdefault("usage", UsageAccumulator())
    usage.calls.extend(result.calls)
    state["refused"] = result.refused
    state["budget_stopped"] = result.budget_stopped
    state["tool_iterations"] = result.tool_iterations

    if result.refused:
        # A refusal degrades the stage; the review still records what it analysed.
        state.setdefault("errors", []).append(StageError("review", "refusal"))
        return state

    state["raw_findings"] = result.report.findings
    return state


async def validate_node(state: ReviewState) -> ReviewState:
    """Deterministic and mandatory.

    Structured output guarantees the shape. It guarantees nothing about whether the
    cited line exists, belongs to this PR, or was even in a file we reviewed.
    """
    raw: list[FindingSchema] = state.get("raw_findings", [])
    kept, dropped = validate_findings(raw, state["pr"])
    state["findings"] = kept
    state.setdefault("dropped", []).extend(dropped)

    if dropped:
        breakdown: dict[str, int] = {}
        for item in dropped:
            breakdown[item.reason] = breakdown.get(item.reason, 0) + 1
        # The drop-rate breakdown is the single most useful diagnostic in this phase:
        # a high outside_diff rate means the context bundle is presenting surrounding
        # context indistinguishably from changed lines.
        logger.info("findings dropped", extra={"breakdown": breakdown})
    return state


MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 8000


def validate_findings(
    findings: list[FindingSchema], pr: PullRequestContext
) -> tuple[list[tuple[FindingSchema, int]], list[DroppedFinding]]:
    """Every rule from the Phase 5 table, in one place.

    Ordered by severity then confidence so that when two findings collide on the
    dedupe key, the one kept is the stronger claim rather than whichever the model
    happened to emit first.
    """
    kept: list[tuple[FindingSchema, int]] = []
    dropped: list[DroppedFinding] = []
    seen: set[tuple[str, int, str]] = set()

    by_path = {f.path: f for f in pr.files}

    for finding in sorted(
        findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), -f.confidence)
    ):
        file = by_path.get(finding.file)
        if file is None:
            dropped.append(DroppedFinding(finding, "unknown_file"))
            continue
        if not file.reviewed:
            dropped.append(DroppedFinding(finding, "file_not_reviewed"))
            continue

        position = file.diff_line_map.get(finding.line)
        if position is None:
            dropped.append(DroppedFinding(finding, "outside_diff"))
            continue
        if finding.line not in file.added_line_numbers:
            # In the diff, but on a context line — code this PR did not touch.
            dropped.append(DroppedFinding(finding, "unchanged_line"))
            continue

        if not finding.title.strip() or not finding.description.strip():
            dropped.append(DroppedFinding(finding, "empty_content"))
            continue

        key = (finding.file, finding.line, finding.category)
        if key in seen:
            dropped.append(DroppedFinding(finding, "duplicate"))
            continue
        seen.add(key)

        # Clamp rather than drop: a malformed confidence is a formatting problem, not
        # evidence that the finding is wrong.
        finding.confidence = min(max(finding.confidence, 0.0), 1.0)
        finding.title = finding.title.strip()[:MAX_TITLE_CHARS]
        finding.description = finding.description.strip()[:MAX_DESCRIPTION_CHARS]
        kept.append((finding, position))

    return kept, dropped


def build_graph() -> Any:
    graph = StateGraph(ReviewState)
    graph.add_node("fetch", fetch_node)
    graph.add_node("build_context", build_context_node)
    graph.add_node("review", review_node)
    graph.add_node("validate", validate_node)

    graph.add_edge(START, "fetch")
    graph.add_edge("fetch", "build_context")
    graph.add_edge("build_context", "review")
    graph.add_edge("review", "validate")
    graph.add_edge("validate", END)
    return graph.compile()


_COMPILED = None


async def run_review_graph(
    *,
    review_id: uuid.UUID,
    pr: PullRequestContext,
    gateway: ToolGateway | None,
) -> ReviewState:
    global _COMPILED
    if _COMPILED is None:
        _COMPILED = build_graph()

    initial: ReviewState = {
        "review_id": str(review_id),
        "pr": pr,
        "gateway": gateway,
        "findings": [],
        "dropped": [],
        "errors": [],
        "usage": UsageAccumulator(),
        "refused": False,
    }
    result: ReviewState = await _COMPILED.ainvoke(initial)
    return result

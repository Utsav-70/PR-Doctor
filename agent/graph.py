"""The review graph — Phases 5 through 8.

    START -> fetch -> build_context -> plan -> review -> validate -> verify -> judge -> END
                                                 |
                                       fans out to bug / security / performance
                                       concurrently, over one shared context bundle

Linear apart from the fan-out. LangGraph is not here for branching; it is here for the
typed state, for the checkpointer Phase 13 needs, and for the pipeline shape being data
rather than nested function calls.

The four stages after the fan-out answer four different questions, and the order is the
point: `validate` asks whether a finding is well-formed, `verify` asks whether it is
true, `judge` asks whether it is worth saying, and Phase 9's guardrails ask whether
saying it is safe.

State is append-only per node, and **a node that fails appends to `errors` and returns
rather than raising** — with one deliberate exception. `judge` produces no judgments on
failure, and nothing downstream posts a finding without one, because a broken PRGuard
posts nothing rather than something wrong.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent.context import ContextBundle, build_tool_context
from agent.evidence import EvidenceRow, verify_findings
from agent.judge import collapse_exact_duplicates, judge_findings
from agent.planner import plan_review
from agent.reviewer import review_diff
from agent.schemas import SEVERITY_ORDER, CallRecord, Judgment, ReviewResult
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
    # Phases 7 and 8. Keyed by index into `findings` after validation, which is the only
    # stable identity a finding has before it reaches the database.
    evidence: dict[int, list[EvidenceRow]]
    verdicts: dict[int, tuple[bool, float, str]]
    agreement: dict[int, list[str]]
    judgments: list[Judgment]
    collapsed: list[tuple[FindingSchema, int]]
    already_posted_keys: set[str]
    # Declared, not stashed under an ad-hoc key: LangGraph propagates only the fields
    # in this schema between nodes, and silently discards anything else. An undeclared
    # hand-off key loses the data with no error at all.
    # (finding, agent that reported it). The agent is carried alongside rather than on
    # the schema: `Finding` is the wire format the provider validates against, and adding
    # a field there would ask the model to fill in something we already know.
    raw_findings: list[tuple[FindingSchema, str]]
    gateway: ToolGateway | None
    context_bundle: ContextBundle | None
    findings: list[tuple[FindingSchema, int]]
    # Parallel to `findings`, index for index, through validation and dedupe.
    finding_agents: list[str]
    agents: list[str]
    focus: dict[str, list[str]]
    plan_rationale: str
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


async def plan_node(state: ReviewState) -> ReviewState:
    """Choose which specialists this change needs. Phase 6."""
    agents, plan, calls = await plan_review(state["pr"])
    state["agents"] = agents
    state["focus"] = plan.focus_map() if plan else {}
    state["plan_rationale"] = plan.rationale if plan else ""
    state.setdefault("usage", UsageAccumulator()).calls.extend(calls)
    if not agents:
        state.setdefault("errors", []).append(
            StageError("plan", "no_agents", "planner selected no agents")
        )
    return state


async def review_node(state: ReviewState) -> ReviewState:
    """Fan out to the selected specialists, concurrently, and merge.

    `asyncio.gather` inside the one Celery task rather than sub-tasks: the review stays a
    single atomic unit with one timeout, one trace and one cost total.

    **Merge concatenates and stamps. It does not deduplicate.** Two agents independently
    reporting the same line is the strongest confidence signal available short of a
    failing test, and collapsing it here would throw that away before the judge can use
    it.
    """
    bundle = state.get("context_bundle")
    if bundle is None:
        state.setdefault("errors", []).append(
            StageError("review", "no_context", "context bundle missing")
        )
        return state

    settings = get_settings()
    agents = state.get("agents") or ["reviewer"]
    focus = state.get("focus", {})

    async def run(agent: str) -> tuple[str, ReviewResult | Exception]:
        try:
            return agent, await review_diff(
                bundle.repository_block,
                bundle.diff_block,
                gateway=state.get("gateway"),
                agent=agent,
                focus=focus.get(agent),
            )
        except Exception as exc:
            return agent, exc

    outcomes = await asyncio.gather(*(run(a) for a in agents))

    usage = state.setdefault("usage", UsageAccumulator())
    merged: list[tuple[FindingSchema, str]] = []
    refusals = 0

    for agent, outcome in outcomes:
        if isinstance(outcome, Exception):
            # Per-agent isolation: a review missing one agent's coverage is recorded as
            # degraded and still worth posting. One prompt hitting a refusal should not
            # cost the other two.
            logger.warning("agent %s failed: %s", agent, outcome)
            state.setdefault("errors", []).append(
                StageError("review", f"{agent}_failed", str(outcome)[:300])
            )
            continue

        usage.calls.extend(outcome.calls)
        state["tool_iterations"] = state.get("tool_iterations", 0) + outcome.tool_iterations
        if outcome.budget_stopped:
            state["budget_stopped"] = outcome.budget_stopped

        if outcome.refused:
            refusals += 1
            state.setdefault("errors", []).append(StageError("review", f"{agent}_refusal"))
            continue

        findings = outcome.report.findings[: settings.MAX_FINDINGS_PER_AGENT]
        if len(outcome.report.findings) > settings.MAX_FINDINGS_PER_AGENT:
            logger.warning(
                "%s produced %d findings; capped at %d",
                agent,
                len(outcome.report.findings),
                settings.MAX_FINDINGS_PER_AGENT,
            )
        merged.extend((f, agent) for f in findings)

    state["refused"] = refusals == len(agents) and bool(agents)
    state["raw_findings"] = merged
    logger.info(
        "merged %d findings from %d/%d agents (%s)",
        len(merged),
        len(agents) - len([e for e in state.get("errors", []) if e.stage == "review"]),
        len(agents),
        ", ".join(agents),
    )
    return state


async def validate_node(state: ReviewState) -> ReviewState:
    """Deterministic and mandatory.

    Structured output guarantees the shape. It guarantees nothing about whether the
    cited line exists, belongs to this PR, or was even in a file we reviewed.
    """
    raw = state.get("raw_findings", [])
    kept, agents, dropped = validate_findings(raw, state["pr"])
    state["findings"] = kept
    state["finding_agents"] = agents
    state.setdefault("dropped", []).extend(dropped)

    if dropped:
        breakdown: dict[str, int] = {}
        for item in dropped:
            breakdown[item.reason] = breakdown.get(item.reason, 0) + 1
        # The drop-rate breakdown is the single most useful diagnostic in this phase:
        # a high outside_diff rate means the context bundle is presenting surrounding
        # context indistinguishably from changed lines.
        logger.info(
            "findings dropped: %s",
            ", ".join(f"{reason}={n}" for reason, n in sorted(breakdown.items())),
        )
    return state


async def verify_node(state: ReviewState) -> ReviewState:
    """Phase 7. Try to prove each finding with something that cannot hallucinate.

    Degrades rather than fails: without evidence the judge holds every finding to the
    unverified bar, which is stricter. A verification outage makes the reviewer quieter,
    never louder.
    """
    findings = state.get("findings", [])
    bundle = state.get("context_bundle")
    gateway = state.get("gateway")
    if not findings or gateway is None or bundle is None:
        return state

    # Exact collisions collapse before anything expensive runs. Independent agreement
    # raises confidence rather than being discarded.
    kept, kept_agents, collapsed, agreement = collapse_exact_duplicates(
        findings, state.get("finding_agents")
    )
    state["findings"] = kept
    state["finding_agents"] = kept_agents
    state["collapsed"] = collapsed
    state["agreement"] = agreement

    try:
        # The diff goes too: without it the verifier is asked about code it cannot see,
        # and every claim about a changed function comes back "cannot verify".
        result = await verify_findings(
            kept,
            gateway=gateway,
            repository_block=f"{bundle.repository_block}\n{bundle.diff_block}",
        )
    except Exception as exc:
        logger.exception("verification failed")
        state.setdefault("errors", []).append(
            StageError("verify", type(exc).__name__, str(exc)[:300])
        )
        return state

    by_finding: dict[int, list[EvidenceRow]] = {}
    for row in result.rows:
        by_finding.setdefault(row.finding_index, []).append(row)
    state["evidence"] = by_finding
    state["verdicts"] = result.verdicts
    state.setdefault("usage", UsageAccumulator()).calls.extend(result.calls)

    if result.capped:
        state.setdefault("errors", []).append(
            StageError("verify", "capped", "verification check cap reached")
        )
    return state


async def judge_node(state: ReviewState) -> ReviewState:
    """Phase 8. The only component that decides what gets posted.

    **Does not degrade.** A judge failure appends an error and produces no judgments,
    and nothing downstream posts a finding without one. A broken PRGuard posts nothing,
    not something wrong — there is no unjudged-fallback path, deliberately.
    """
    findings = state.get("findings", [])
    bundle = state.get("context_bundle")
    if not findings or bundle is None:
        state["judgments"] = []
        return state

    try:
        judgments, calls = await judge_findings(
            findings,
            evidence=state.get("evidence", {}),
            verdicts=state.get("verdicts", {}),
            agreement=state.get("agreement", {}),
            agents=state.get("finding_agents"),
            repository_block=f"{bundle.repository_block}\n{bundle.diff_block}",
            already_posted_keys=state.get("already_posted_keys", set()),
        )
    except Exception as exc:
        logger.exception("judge failed")
        state["judgments"] = []
        state.setdefault("errors", []).append(
            StageError("judge", type(exc).__name__, str(exc)[:300])
        )
        return state

    state["judgments"] = judgments
    state.setdefault("usage", UsageAccumulator()).calls.extend(calls)
    return state


MAX_TITLE_CHARS = 200
MAX_DESCRIPTION_CHARS = 8000


def validate_findings(
    findings: list[tuple[FindingSchema, str]], pr: PullRequestContext
) -> tuple[list[tuple[FindingSchema, int]], list[str], list[DroppedFinding]]:
    """Every rule from the Phase 5 table, in one place.

    Ordered by severity then confidence so that when two findings collide on the
    dedupe key, the one kept is the stronger claim rather than whichever the model
    happened to emit first.

    Returns (kept, reporting agent per kept finding, dropped). The agent list is parallel
    to `kept` rather than embedded, because the sort below reorders and a mapping keyed on
    the input position would silently point at the wrong finding.

    Note the `duplicate` rule here is *within one agent's own output* and across agents
    only for byte-identical (file, line, category). Cross-agent agreement on the same
    issue is not collapsed here — Phase 8 treats it as a confidence signal first.
    """
    kept: list[tuple[FindingSchema, int]] = []
    kept_agents: list[str] = []
    dropped: list[DroppedFinding] = []
    seen: set[tuple[str, int, str]] = set()

    by_path = {f.path: f for f in pr.files}

    for finding, agent in sorted(
        findings, key=lambda fa: (SEVERITY_ORDER.get(fa[0].severity, 9), -fa[0].confidence)
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
        kept_agents.append(agent)

        # Clamp rather than drop: a malformed confidence is a formatting problem, not
        # evidence that the finding is wrong.
        finding.confidence = min(max(finding.confidence, 0.0), 1.0)
        finding.title = finding.title.strip()[:MAX_TITLE_CHARS]
        finding.description = finding.description.strip()[:MAX_DESCRIPTION_CHARS]
        kept.append((finding, position))

    return kept, kept_agents, dropped


def build_graph() -> Any:
    graph = StateGraph(ReviewState)
    graph.add_node("fetch", fetch_node)
    graph.add_node("build_context", build_context_node)
    graph.add_node("review", review_node)
    graph.add_node("plan", plan_node)
    graph.add_node("validate", validate_node)
    graph.add_node("verify", verify_node)
    graph.add_node("judge", judge_node)

    graph.add_edge(START, "fetch")
    graph.add_edge("fetch", "build_context")
    # Planning comes after the context bundle so the digest reflects what was actually
    # reviewable, and before the fan-out because it decides how wide that fan-out is.
    graph.add_edge("build_context", "plan")
    graph.add_edge("plan", "review")
    graph.add_edge("review", "validate")
    # Validation proves the finding is well-formed; verification tries to prove it is
    # true; the judge decides whether it is worth saying. Three different questions, in
    # the only order that makes sense.
    graph.add_edge("validate", "verify")
    graph.add_edge("verify", "judge")
    graph.add_edge("judge", END)
    return graph.compile()


_COMPILED = None


async def run_review_graph(
    *,
    review_id: uuid.UUID,
    pr: PullRequestContext,
    gateway: ToolGateway | None,
    already_posted_keys: set[str] | None = None,
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
        "already_posted_keys": already_posted_keys or set(),
    }
    result: ReviewState = await _COMPILED.ainvoke(initial)
    return result

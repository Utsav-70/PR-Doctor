"""Phase 6 — the planner.

Cheap routing, not analysis. Its main job is deciding which agents *not* to run: a
docs-only change needs none, a dependency bump needs security and nothing else. Skipping
an agent is the cheapest optimisation available and the only one that saves a whole call.

**The planner narrows; it never overrides a floor.** A planner that decides to skip the
security agent on `auth.py` is not a suboptimal plan, it is a vulnerability shipped — so
`apply_floors` runs after every plan and can only add agents back, never remove them.

Every skip is logged. A skipped agent is a recall loss and nothing measures recall until
Phase 16, so the log is the only trail there is.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Literal

from pydantic import BaseModel, Field

from agent.schemas import CallRecord
from domain import PullRequestContext
from settings import get_settings

logger = logging.getLogger(__name__)

AgentName = Literal["bug", "security", "performance"]
ALL_AGENTS: tuple[str, ...] = ("bug", "security", "performance")

# Paths that force the security agent regardless of the plan. Deliberately broad: a false
# positive here costs one extra call, a false negative costs a missed vulnerability.
SECURITY_PATH_HINTS = re.compile(
    r"(auth|login|session|token|password|credential|secret|crypto|permission|"
    r"acl|role|admin|payment|billing|checkout|oauth|saml|jwt|sql|query|migration)",
    re.IGNORECASE,
)


class AgentFocus(BaseModel):
    agent: AgentName
    paths: list[str] = Field(
        description="File paths from the diff, in the order this agent should read them"
    )


class ReviewPlan(BaseModel):
    """The routing decision.

    `focus` is a list rather than a `dict[agent, paths]`: a mapping renders as
    `additionalProperties` in JSON Schema, which the Gemini Developer API rejects
    outright. The list also pins the key to the `AgentName` literal, so the model cannot
    invent an agent that does not exist.
    """

    run_agents: list[AgentName] = Field(
        description="Which specialists this change actually needs. Omit the ones it does not."
    )
    focus: list[AgentFocus] = Field(default_factory=list)
    rationale: str = Field(
        description="One or two sentences on why these agents and not the others"
    )

    def focus_map(self) -> dict[str, list[str]]:
        return {str(item.agent): item.paths for item in self.focus}


PLANNER_PROMPT = """You route a pull request to code-review specialists. You do not review \
the code.

Three specialists are available:

- `bug` — correctness: logic errors, edge cases, exception paths, races, idempotency.
- `security` — injection, auth and authorization, secrets, deserialization, crypto,
  dependency changes.
- `performance` — N+1 queries, blocking IO in async code, memory growth, complexity.

Run only the ones this change plausibly needs. A documentation-only change needs none. A \
pure test addition usually needs `bug` alone. A lockfile bump needs `security` alone.

Running an unnecessary agent wastes a call. Skipping a necessary one loses a real defect \
permanently, and nothing downstream can recover it. When genuinely unsure, include the \
agent.

In `focus`, order the files each agent should read first — the security agent wants the \
authentication and payment files, the performance agent wants the file with the new loop. \
Use only paths that appear in the diff."""


def _render_digest(pr: PullRequestContext) -> str:
    """The file list and a shape summary — not the diff.

    The planner decides on structure, and giving it the full diff would cost most of what
    skipping an agent saves.
    """
    lines = [
        f"<pull_request title={pr.title[:200]!r} "
        f"files={pr.changed_files} added={pr.added_lines} deleted={pr.deleted_lines}>"
    ]
    for file in sorted(pr.reviewed_files, key=lambda f: f.path):
        lines.append(
            f'  <file path="{file.path}" language="{file.language}" '
            f'category="{file.category}" change="{file.change_type}" '
            f"+{file.additions} -{file.deletions}/>"
        )
    for file in sorted(pr.skipped_files, key=lambda f: f.path):
        lines.append(f'  <skipped path="{file.path}" reason="{file.skip_reason}"/>')
    lines.append("</pull_request>")
    return "\n".join(lines)


def apply_floors(planned: list[str], pr: PullRequestContext) -> tuple[list[str], list[str]]:
    """Add back agents that must run, then apply the config filter.

    Returns (agents_to_run, reasons_added). Order of operations matters: the floors are
    the safety property, `ENABLED_AGENTS` is an operator's explicit instruction, and the
    operator wins — otherwise turning an agent off would not turn it off.
    """
    settings = get_settings()
    agents = set(planned)
    added: list[str] = []

    if "security" not in agents:
        sensitive = [
            f.path
            for f in pr.reviewed_files
            if SECURITY_PATH_HINTS.search(f.path) or f.category == "dependency"
        ]
        if sensitive:
            agents.add("security")
            added.append(f"security (path floor: {', '.join(sensitive[:3])})")

    if "bug" not in agents and any(f.category == "source" for f in pr.reviewed_files):
        agents.add("bug")
        added.append("bug (source floor)")

    enabled = set(settings.enabled_agents)
    filtered = [a for a in ALL_AGENTS if a in agents and a in enabled]
    if removed := sorted(agents - enabled):
        logger.info("agents disabled by config: %s", ", ".join(removed))
    return filtered, added


async def plan_review(pr: PullRequestContext) -> tuple[list[str], ReviewPlan | None, list[CallRecord]]:
    """Choose the agents. Falls back to all of them if the planner cannot answer.

    Failing open is correct here and nowhere else in this pipeline: the planner exists to
    save money, and a planner outage should cost money rather than coverage.
    """
    settings = get_settings()
    calls: list[CallRecord] = []

    if not settings.ENABLE_PLANNER:
        agents, _ = apply_floors(list(ALL_AGENTS), pr)
        return agents, None, calls

    plan = await _call_planner(_render_digest(pr), calls)

    if plan is None:
        agents, _ = apply_floors(list(ALL_AGENTS), pr)
        logger.warning("planner unavailable; running all enabled agents")
        return agents, None, calls

    agents, added = apply_floors([str(a) for a in plan.run_agents], pr)
    skipped = [a for a in ALL_AGENTS if a not in agents]
    logger.info(
        "plan: run %s%s%s — %s",
        ", ".join(agents) or "none",
        f" | floors added {'; '.join(added)}" if added else "",
        f" | skipped {', '.join(skipped)}" if skipped else "",
        plan.rationale[:200],
    )
    return agents, plan, calls


async def _call_planner(digest: str, calls: list[CallRecord]) -> ReviewPlan | None:
    """Medium effort. This is routing, and paying reviewer-grade reasoning for it would
    cost more than the agent it skips."""
    from agent.reviewer import estimate_cost

    settings = get_settings()
    started = time.perf_counter()

    try:
        if settings.LLM_PROVIDER == "gemini":
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=settings.GEMINI_API_KEY,
                http_options=types.HttpOptions(timeout=int(settings.LLM_TIMEOUT_SECONDS * 1000)),
            )
            response = await client.aio.models.generate_content(
                model=settings.LLM_MODEL,
                contents=[digest],
                config=types.GenerateContentConfig(
                    system_instruction=PLANNER_PROMPT,
                    max_output_tokens=4096,
                    response_mime_type="application/json",
                    response_schema=ReviewPlan,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    thinking_config=types.ThinkingConfig(thinking_budget=2048),
                ),
            )
            meta = response.usage_metadata
            input_tokens = (meta.prompt_token_count or 0) if meta else 0
            output_tokens = (
                ((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0))
                if meta
                else 0
            )
            cost = estimate_cost(
                settings.LLM_MODEL, input_tokens, output_tokens, 0, 0, cache_read_multiplier=0.25
            )
            parsed = response.parsed
            provider = "gemini"
        else:
            from anthropic import AsyncAnthropic

            client_a = AsyncAnthropic(
                api_key=settings.ANTHROPIC_API_KEY,
                timeout=settings.LLM_TIMEOUT_SECONDS,
                max_retries=settings.LLM_MAX_RETRIES,
            )
            try:
                message = await client_a.messages.parse(
                    model=settings.LLM_MODEL,
                    max_tokens=4096,
                    system=[
                        {
                            "type": "text",
                            "text": PLANNER_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    output_format=ReviewPlan,
                    output_config={"effort": "medium"},
                    messages=[{"role": "user", "content": digest}],
                )
            finally:
                await client_a.close()
            input_tokens = message.usage.input_tokens
            output_tokens = message.usage.output_tokens
            cost = estimate_cost(
                settings.LLM_MODEL,
                input_tokens,
                output_tokens,
                message.usage.cache_read_input_tokens or 0,
                message.usage.cache_creation_input_tokens or 0,
            )
            parsed = message.parsed_output
            provider = "anthropic"
    except Exception:
        logger.exception("planner call failed")
        calls.append(
            CallRecord(
                stage="plan",
                provider=settings.LLM_PROVIDER,
                model=settings.LLM_MODEL,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error="planner call failed",
            )
        )
        return None

    calls.append(
        CallRecord(
            stage="plan",
            provider=provider,
            model=settings.LLM_MODEL,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    )
    return parsed if isinstance(parsed, ReviewPlan) else None

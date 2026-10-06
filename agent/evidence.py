"""Phase 7 — evidence verification.

**The LLM proposes; deterministic tooling verifies.**

A reviewer saying "SQL injection at line 87, confidence 0.9" has stated a hypothesis with
a number attached, and that number is self-assessed. A wrong finding and a right finding
both come back at 0.9. Posting on self-reported confidence means posting confident
nonsense, and two of those per PR is how a review bot gets muted.

Two calls per batch, and the split is the phase's most important structural decision:

    1. plan        given the findings, choose checks that *could refute them*
    2. verdict     given what the checks actually returned, conclude

One call that both plans and concludes lets the model reason its way to the verdict it
started with. Separating them forces the conclusion to arrive after the facts.

Batched by file: findings in the same file share context, and the context is already in
the cached prefix.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from agent.schemas import (
    EVIDENCE_WEIGHT,
    CallRecord,
    Check,
    EvidenceVerdicts,
    VerificationPlan,
)
from agent.schemas import Finding as FindingSchema
from agent.tools.gateway import ToolGateway
from settings import get_settings

logger = logging.getLogger(__name__)

# A planned method maps to the gateway tool that answers it. `ast` and `dataflow` are not
# separate tools — they are the structural and reference-tracing questions that
# get_symbol and find_references already answer, named for what they prove rather than
# for how they are implemented.
METHOD_TOOL: dict[str, str] = {
    "search": "search_code",
    "ast": "get_symbol",
    "dataflow": "find_references",
    "tests": "get_tests",
}

# Planned but not yet runnable. Recorded as `error` with this reason rather than silently
# dropped: verification coverage that is degraded should be visible, not invisible.
UNAVAILABLE_UNTIL_PHASE_10 = frozenset({"semgrep", "ruff", "bandit"})

# A refutation is worth more than a confirmation. Proving a query is parameterised kills
# a SQL-injection claim outright; confirming an f-string only makes it plausible.
# ALPHA and BETA are placeholders until Phase 16 tunes them against measured outcomes;
# the asymmetry between them is the part that is not a guess.
SUPPORT_ALPHA = 0.35
REFUTE_BETA = 1.10

# Above this, a single refuting check vetoes the finding no matter what the judge says.
# Phase 8 enforces the veto; this is where the bar is defined.
STRONG_REFUTATION_WEIGHT = 0.80


@dataclass
class EvidenceRow:
    """One executed check. Persisted verbatim — refutations especially.

    The set of refuted findings is the highest-signal dataset in the system: it is where
    false-positive analysis starts, and it is how a prompt regression is told apart from
    a tooling regression.
    """

    finding_index: int
    method: str
    outcome: str = "inconclusive"
    weight: float = 0.0
    rationale: str = ""
    detail: str = ""
    duration_ms: int = 0


@dataclass
class VerificationResult:
    rows: list[EvidenceRow] = field(default_factory=list)
    # finding index -> (verified, verified_confidence, reasoning)
    verdicts: dict[int, tuple[bool, float, str]] = field(default_factory=dict)
    calls: list[CallRecord] = field(default_factory=list)
    checks_run: int = 0
    checks_cached: int = 0
    capped: bool = False

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)


def adjust_confidence(agent_confidence: float, rows: list[EvidenceRow]) -> float:
    """Fold evidence into the reported confidence.

        s = sum(supporting weights) * ALPHA
        supported = c + (1 - c) * s / (1 + s)          saturating, never reaches 1
        verified  = supported * (1 - sum(refuting) * BETA)

    Support **closes a fraction of the gap to certainty** rather than scaling the
    confidence. A linear `c * (1 + s)` was wrong in a way the [0,1] clamp concealed: one
    0.60-weight text match took 0.90 to exactly 1.0, so a grep hit produced certainty.
    The saturating form gives the second supporting check less than the first and can
    never reach 1.0 — which is the honest shape, since no amount of corroboration makes
    a static-analysis claim certain.

    Refutation stays multiplicative because it *should* be able to crush: a single AST
    fact showing the query is parameterised takes 0.90 to 0.06.

    Errored and inconclusive checks move nothing in either direction. `inconclusive`
    means "unproven", not "disproven", and treating the two alike silently discards real
    bugs that happen to be hard to prove.
    """
    supporting = sum(r.weight for r in rows if r.outcome == "supports") * SUPPORT_ALPHA
    refuting = sum(r.weight for r in rows if r.outcome == "refutes")

    value = agent_confidence
    if supporting > 0:
        value += (1.0 - value) * (supporting / (1.0 + supporting))
    value *= 1.0 - refuting * REFUTE_BETA
    return max(0.0, min(1.0, value))


def has_strong_refutation(rows: list[EvidenceRow]) -> bool:
    return any(
        r.outcome == "refutes" and r.weight >= STRONG_REFUTATION_WEIGHT for r in rows
    )


def _render_findings(findings: list[tuple[int, FindingSchema]]) -> str:
    parts = []
    for index, finding in findings:
        parts.append(
            f"<finding index=\"{index}\" file=\"{finding.file}\" line=\"{finding.line}\" "
            f'severity="{finding.severity}" category="{finding.category}" '
            f'reported_confidence="{finding.confidence:.2f}">\n'
            f"{finding.title}\n{finding.description}\n</finding>"
        )
    return "\n".join(parts)


PLAN_PROMPT = """You decide which checks would settle a code-review claim. You do not \
re-review the code and you do not decide whether the claims are true.

For the findings below, choose checks that could **refute** them, not only confirm them. \
A check that can only agree is not verification.

Methods available:

- `search` — regex across the repository. Set `pattern`, optionally `path_glob`.
  Proves a string is present or absent. Use for "is there a unique constraint", "does any
  other call site do X".
- `ast` — resolve a definition and read its real signature and body. Set `name`. Use to
  check what a called function actually does, whether a parameter is optional, whether an
  `execute` call takes parameters.
- `dataflow` — every real use of a symbol. Set `name`. Use to trace where a value
  originates, or whether any caller passes None. Heuristic: no type inference.
- `tests` — tests related to a file or symbol. Set `path` or `symbol`.

Set only the fields your chosen method uses; leave the rest null.

Good checks are specific. `search` for `idempotency_key` is useful; `search` for `def` is
not. Prefer two decisive checks over five vague ones.

**Do not plan a check that only re-reads the diff.** Searching for the very line the
finding quotes confirms the premise, not the claim, and it comes back looking like
evidence when it is not.

**These checks see only this repository.** Third-party library source is not available. If
a finding turns on what an external library does with its argument, no check here can
settle it — plan nothing for it and let it be judged unverified.

Return at most {max_checks} checks total across all findings."""


VERDICT_PROMPT = """You decide whether each claim survived its checks.

You are given findings and the **actual output** of checks that were run against the \
repository. Judge only from that output.

For each finding return:

- `verified` — true only if a check positively confirmed the mechanism of the claim.
- `confidence` — your honest probability the claim is real, given the evidence. Lower it
  when checks came back empty; a check that found nothing is weak evidence against.
- `supporting` / `refuting` — which checks pointed which way, and what they showed.

Refuting evidence is decisive. If a check shows the query is parameterised, the loop runs \
over a literal, or the caller never passes None, say so and set confidence low.

Empty output is not refutation by itself — a search that matched nothing may mean the \
pattern was wrong. Say `inconclusive` in your reasoning when that is the honest read.

## Two rules that decide most verdicts

**Confirming the code says what the finding says it says is not support.** A finding \
claiming `timeout=x * 1000` is a unit bug is not corroborated by a search that finds \
`timeout=x * 1000`. That only confirms the premise, which was never in doubt. Support \
means a check established the *defect* — that the value is wrong, that the caller passes \
None, that the loop is unbounded. If every check merely re-read the diff, the honest \
verdict is `verified: false` with confidence no higher than reported.

**You can only see this repository.** Third-party library code is not available to these \
checks. A claim that turns on what an external library does with its argument — what unit \
it expects, what it raises, whether it retries — **cannot be verified here**. Say so, set \
`verified: false`, and lower confidence: a claim about an unreadable contract, asserted \
confidently, is where false positives come from."""


async def verify_findings(
    findings: list[tuple[FindingSchema, int]],
    *,
    gateway: ToolGateway,
    repository_block: str,
) -> VerificationResult:
    """Plan, execute, and conclude — batched per file.

    Returns evidence rows keyed to the finding's index in `findings`, so the caller can
    attach them without re-matching on content.
    """
    settings = get_settings()
    result = VerificationResult()
    if not findings:
        return result

    # Below-threshold severities are recorded but never posted, so verifying them buys
    # nothing and verification is the easiest stage to let run away on cost.
    from agent.schemas import SEVERITY_ORDER

    floor = SEVERITY_ORDER[settings.SKIP_VERIFY_BELOW_SEVERITY]
    eligible = [
        (i, f) for i, (f, _) in enumerate(findings) if SEVERITY_ORDER.get(f.severity, 9) <= floor
    ]
    if not eligible:
        logger.info("verification skipped: no findings at or above %s",
                    settings.SKIP_VERIFY_BELOW_SEVERITY)
        return result

    by_file: dict[str, list[tuple[int, FindingSchema]]] = {}
    for index, finding in eligible:
        by_file.setdefault(finding.file, []).append((index, finding))

    # Within one review the same check is often planned twice — two findings on adjacent
    # lines ask the same question. Keyed on (method, args), so the second asker pays
    # nothing.
    cache: dict[str, str] = {}

    for path in sorted(by_file):
        if result.checks_run >= settings.MAX_VERIFICATION_CHECKS_PER_REVIEW:
            result.capped = True
            logger.warning(
                "verification capped at %d checks; %d findings left unverified",
                settings.MAX_VERIFICATION_CHECKS_PER_REVIEW,
                len(by_file[path]),
            )
            break
        await _verify_file(
            by_file[path],
            gateway=gateway,
            repository_block=repository_block,
            result=result,
            cache=cache,
        )

    logger.info(
        "verification complete: %d checks (%d cached), %d verdicts, $%.4f%s",
        result.checks_run,
        result.checks_cached,
        len(result.verdicts),
        result.cost_usd,
        " [CAPPED]" if result.capped else "",
    )
    return result


async def _verify_file(
    batch: list[tuple[int, FindingSchema]],
    *,
    gateway: ToolGateway,
    repository_block: str,
    result: VerificationResult,
    cache: dict[str, str],
) -> None:
    settings = get_settings()
    rendered = _render_findings(batch)

    plan = await _call_structured(
        system=PLAN_PROMPT.replace("{max_checks}", str(settings.MAX_CHECKS_PER_BATCH)),
        user=[repository_block, rendered],
        schema=VerificationPlan,
        stage="verify_plan",
        result=result,
    )
    if plan is None:
        for index, _ in batch:
            result.rows.append(
                EvidenceRow(index, "plan", "error", 0.0, detail="planning call failed")
            )
        return

    executed: list[tuple[Check, EvidenceRow, str]] = []
    for check in plan.checks[: settings.MAX_CHECKS_PER_BATCH]:
        if result.checks_run >= settings.MAX_VERIFICATION_CHECKS_PER_REVIEW:
            result.capped = True
            break
        row, payload = await _run_check(check, gateway=gateway, cache=cache, result=result)
        executed.append((check, row, payload))

    if not executed:
        for index, _ in batch:
            result.rows.append(
                EvidenceRow(index, "plan", "inconclusive", 0.0, detail="no checks were run")
            )
        return

    evidence_block = "\n".join(
        f'<check method="{c.method}" rationale="{c.rationale}" '
        f"args={json.dumps(c.tool_args(), sort_keys=True)}>\n{payload}\n</check>"
        for c, _row, payload in executed
    )

    verdicts = await _call_structured(
        system=VERDICT_PROMPT,
        user=[repository_block, rendered, f"<evidence>\n{evidence_block}\n</evidence>"],
        schema=EvidenceVerdicts,
        stage="verify_verdict",
        result=result,
    )

    indices = {index for index, _ in batch}
    for _check, row, _payload in executed:
        # A check settles every finding in its batch — they were planned together.
        for index in sorted(indices):
            result.rows.append(
                EvidenceRow(
                    finding_index=index,
                    method=row.method,
                    outcome=row.outcome,
                    weight=row.weight,
                    rationale=row.rationale,
                    detail=row.detail,
                    duration_ms=row.duration_ms,
                )
            )

    if verdicts is None:
        return

    for verdict in verdicts.verdicts:
        if verdict.finding_index not in indices:
            # The model invented an index. Dropping it is correct: attaching a verdict to
            # the wrong finding is worse than leaving one unverified.
            logger.warning("verdict for unknown finding index %d", verdict.finding_index)
            continue
        # The verdict names which checks pointed which way; the rows carry the weights.
        # Fold them together so an outcome the model asserted has a weight behind it.
        _apply_outcomes(result, verdict.finding_index, verdict.supporting, verdict.refuting)
        rows = [r for r in result.rows if r.finding_index == verdict.finding_index]
        finding = next(f for i, f in batch if i == verdict.finding_index)
        adjusted = adjust_confidence(min(verdict.confidence, finding.confidence), rows)
        result.verdicts[verdict.finding_index] = (verdict.verified, adjusted, verdict.reasoning)


def _apply_outcomes(
    result: VerificationResult, index: int, supporting: list[str], refuting: list[str]
) -> None:
    """Mark each of this finding's evidence rows by what the verdict said about it.

    Matched on the method name appearing in the verdict's own prose, which is loose — but
    the alternative is asking the model to echo check IDs, which it does unreliably and
    which fails silently when it gets one wrong.
    """
    support_text = " ".join(supporting).lower()
    refute_text = " ".join(refuting).lower()
    for row in result.rows:
        if row.finding_index != index or row.outcome == "error":
            continue
        if row.method in refute_text:
            row.outcome = "refutes"
        elif row.method in support_text:
            row.outcome = "supports"
        else:
            row.outcome = "inconclusive"
        row.weight = EVIDENCE_WEIGHT.get(row.method, 0.0) if row.outcome != "inconclusive" else 0.0


async def _run_check(
    check: Check,
    *,
    gateway: ToolGateway,
    cache: dict[str, str],
    result: VerificationResult,
) -> tuple[EvidenceRow, str]:
    started = time.perf_counter()
    row = EvidenceRow(
        finding_index=-1, method=check.method, rationale=check.rationale[:500]
    )

    if check.method in UNAVAILABLE_UNTIL_PHASE_10:
        row.outcome = "error"
        row.detail = f"{check.method} not available until Phase 10"
        return row, row.detail

    tool = METHOD_TOOL.get(check.method)
    if tool is None:
        row.outcome = "error"
        row.detail = f"unknown method {check.method}"
        return row, row.detail

    args = check.tool_args()
    key = f"{tool}:{json.dumps(args, sort_keys=True)}"
    if key in cache:
        result.checks_cached += 1
        row.detail = cache[key][:2000]
        row.duration_ms = 0
        return row, cache[key]

    outcome = await gateway.call("judge", tool, args)
    result.checks_run += 1
    payload = outcome.model_dump_json()[:6000]
    cache[key] = payload
    row.detail = payload[:2000]
    row.duration_ms = int((time.perf_counter() - started) * 1000)
    if not outcome.ok:
        row.outcome = "error"
    return row, payload


async def _call_structured(
    *,
    system: str,
    user: list[str],
    schema: type[Any],
    stage: str,
    result: VerificationResult,
) -> Any:
    """One structured-output call, provider-switched, recorded as an `llm_calls` row.

    Returns None on failure rather than raising: a verification stage that cannot run
    should leave findings unverified for the judge to handle at the higher bar, not lose
    the whole review.
    """
    from agent.reviewer import estimate_cost

    settings = get_settings()
    started = time.perf_counter()

    try:
        if settings.LLM_PROVIDER == "gemini":
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=settings.GEMINI_API_KEY,
                http_options=types.HttpOptions(
                    timeout=int(settings.LLM_TIMEOUT_SECONDS * 1000)
                ),
            )
            response = await client.aio.models.generate_content(
                model=settings.LLM_MODEL,
                contents=list(user),
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    max_output_tokens=settings.LLM_MAX_TOKENS,
                    response_mime_type="application/json",
                    response_schema=schema,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=_evidence_thinking_budget(settings.LLM_MODEL)
                    ),
                ),
            )
            meta = response.usage_metadata
            input_tokens = (meta.prompt_token_count or 0) if meta else 0
            output_tokens = (
                ((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0))
                if meta
                else 0
            )
            cache_read = (meta.cached_content_token_count or 0) if meta else 0
            cost = estimate_cost(
                settings.LLM_MODEL, input_tokens, output_tokens, cache_read, 0,
                cache_read_multiplier=0.25,
            )
            parsed = response.parsed
            provider = "gemini"
        else:
            from anthropic import AsyncAnthropic

            # The SDK's TypedDicts are stricter than the shape built here; it is correct
            # at runtime, same as the investigation loop's tool list.
            content: Any = [
                {"type": "text", "text": user[0], "cache_control": {"type": "ephemeral"}},
                *[{"type": "text", "text": t} for t in user[1:]],
            ]
            client_a = AsyncAnthropic(
                api_key=settings.ANTHROPIC_API_KEY,
                timeout=settings.LLM_TIMEOUT_SECONDS,
                max_retries=settings.LLM_MAX_RETRIES,
            )
            try:
                message = await client_a.messages.parse(
                    model=settings.LLM_MODEL,
                    max_tokens=settings.LLM_MAX_TOKENS,
                    system=[
                        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                    ],
                    thinking={"type": "adaptive"},
                    output_format=schema,
                    output_config={"effort": "medium"},
                    messages=[{"role": "user", "content": content}],
                )
            finally:
                await client_a.close()
            input_tokens = message.usage.input_tokens
            output_tokens = message.usage.output_tokens
            cache_read = message.usage.cache_read_input_tokens or 0
            cost = estimate_cost(
                settings.LLM_MODEL, input_tokens, output_tokens, cache_read,
                message.usage.cache_creation_input_tokens or 0,
            )
            parsed = message.parsed_output
            provider = "anthropic"
    except Exception:
        logger.exception("%s call failed", stage)
        result.calls.append(
            CallRecord(
                stage=stage,
                provider=settings.LLM_PROVIDER,
                model=settings.LLM_MODEL,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error="call failed",
            )
        )
        return None

    result.calls.append(
        CallRecord(
            stage=stage,
            provider=provider,
            model=settings.LLM_MODEL,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cost_usd=cost,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    )
    return parsed if isinstance(parsed, schema) else None


def _evidence_thinking_budget(model: str) -> int:
    """Medium effort: the work is choosing checks and reading tool output, not open-ended
    analysis. Spending reviewer-level reasoning here is the fastest way to make
    verification cost more than the review it verifies."""
    return 4096 if "flash" in model else 8192

"""Phase 8 — the judge.

One component decides what gets posted. Everything upstream produces candidates.

The reviewers are told to report everything and filter nothing, because a reviewer that
self-censors finds bugs and stays quiet about them. That instruction is only safe if
something downstream does the filtering. This is that something.

**One call for the whole review, never per finding.** Duplicate detection, contradiction
detection and volume control are all cross-finding decisions, and none of them are
possible with one finding in view.

The judge is an LLM and can be wrong, so a thin deterministic layer surrounds it — see
`_enforce`. Its rules override the judge in one direction only: they can reject what the
judge accepted, never the reverse.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time

from agent.evidence import EvidenceRow, has_strong_refutation
from agent.schemas import SEVERITY_ORDER, CallRecord, JudgeVerdict, Judgment
from agent.schemas import Finding as FindingSchema
from settings import get_settings

logger = logging.getLogger(__name__)


class JudgeError(RuntimeError):
    """The judge could not reach a verdict.

    Raised rather than degraded on purpose. A broken PRGuard posts nothing, not something
    wrong — there is no "post unjudged findings" fallback.
    """


_WHITESPACE = re.compile(r"\s+")


def dedupe_key(finding: FindingSchema) -> str:
    """Hash of (file, line, category, normalised title).

    A deterministic pre-pass so exact collisions collapse before the LLM sees them. It
    keeps the judge's input smaller and the decision cheaper; near-duplicates are the
    judge's job, not this function's.
    """
    title = _WHITESPACE.sub(" ", finding.title.strip().lower())
    raw = f"{finding.file}|{finding.line}|{finding.category}|{title}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def collapse_exact_duplicates(
    findings: list[tuple[FindingSchema, int]],
    agents: list[str] | None = None,
) -> tuple[
    list[tuple[FindingSchema, int]],
    list[str],
    list[tuple[FindingSchema, int]],
    dict[int, list[str]],
]:
    """Merge exact collisions, raising confidence for independent agreement.

    Two agents finding the same thing is the strongest signal available short of a
    failing test. Merge and credit both — discarding the second throws that away.

    **Agreement only counts when it is independent.** The same agent reporting a finding
    twice is one opinion repeated, not two opinions converging, so it collapses without a
    confidence bump. Conflating the two would let a single repetitive agent manufacture
    the strongest signal the system has.

    Returns (kept, agent per kept finding, collapsed, agreeing agents by kept index).
    """
    names = agents or ["reviewer"] * len(findings)
    kept: list[tuple[FindingSchema, int]] = []
    kept_agents: list[str] = []
    collapsed: list[tuple[FindingSchema, int]] = []
    seen: dict[str, int] = {}
    agreement: dict[int, list[str]] = {}

    for (finding, position), agent in zip(findings, names, strict=False):
        key = dedupe_key(finding)
        if key not in seen:
            seen[key] = len(kept)
            kept.append((finding, position))
            kept_agents.append(agent)
            continue

        index = seen[key]
        collapsed.append((finding, position))
        contributors = agreement.setdefault(index, [kept_agents[index]])
        if agent in contributors:
            continue
        contributors.append(agent)
        # Diminishing returns: the second agent is worth a lot, the fourth very little,
        # and agreement alone must never reach certainty.
        bump = 1 - (1 - kept[index][0].confidence) * 0.5
        kept[index][0].confidence = min(0.99, max(kept[index][0].confidence, bump))

    return kept, kept_agents, collapsed, agreement


JUDGE_PROMPT = """You decide which code-review findings reach a developer. You are the \
only filter; the reviewers were instructed to report everything and filter nothing.

Twenty candidates becoming a handful is the expected outcome. A judge that accepts most \
of its input is not judging.

## Reject

- `duplicate` — the same underlying issue as another finding, including the same bug
  reported at two lines of one expression, or framed as BUG by one agent and RELIABILITY
  by another. Set `duplicate_of` to the index you are collapsing into.
- `false_positive` — the evidence refutes it. The reviewer was wrong.
- `weak_evidence` — no supporting evidence on a claim whose category had a strong check
  available. Different from `false_positive`: this means we could not tell.
- `irrelevant` — real but not worth saying. Reject: style, naming, formatting, import
  order; pre-existing issues on lines this PR did not change; test-style comments;
  complexity claims about provably small collections; defensive checks for states that
  cannot occur; anything a linter already reports.
- `below_threshold` — passed everything else but is too low-confidence or too low-severity
  to be worth a developer's attention.

## Accept

A finding you would be comfortable defending to the author. For each, set `severity` to
what you consider correct — not what was reported. Reporting agents anchor high.

Use `revised_description` when the description is correct but overstated. "This will
corrupt the database" for something that raises a handled exception should be toned down,
not rejected. Leave it null if the original is accurate.

## Evidence

Each finding carries the checks that were run and what they showed. Weight them: a test
result or an AST fact is strong; a text match proves presence, not semantics. Evidence
marked `inconclusive` means unproven — it is not refutation, and it is not support.

Judge every finding you are given. A finding you omit is rejected by default."""


def _render_for_judge(
    findings: list[tuple[FindingSchema, int]],
    evidence: dict[int, list[EvidenceRow]],
    verdicts: dict[int, tuple[bool, float, str]],
    agreement: dict[int, list[str]],
    agents: list[str] | None = None,
) -> str:
    parts = []
    names = agents or []
    for index, (finding, _position) in enumerate(findings):
        rows = evidence.get(index, [])
        verified, adjusted, reasoning = verdicts.get(index, (False, finding.confidence, ""))
        lines = [
            f'<finding index="{index}" file="{finding.file}" line="{finding.line}" '
            f'severity="{finding.severity}" category="{finding.category}" '
            f'reported_confidence="{finding.confidence:.2f}" '
            f'verified="{str(verified).lower()}" '
            f'verified_confidence="{adjusted:.2f}"'
            + (f' reported_by="{names[index]}"' if index < len(names) else "")
            + (
                f' independently_agreed_by="{",".join(agreement[index])}"'
                if index in agreement
                else ""
            )
            + ">",
            finding.title,
            finding.description,
        ]
        if reasoning:
            lines.append(f"<verification_reasoning>{reasoning}</verification_reasoning>")
        for row in rows:
            lines.append(
                f'<evidence method="{row.method}" outcome="{row.outcome}" '
                f'weight="{row.weight:.2f}">{row.detail[:600]}</evidence>'
            )
        lines.append("</finding>")
        parts.append("\n".join(lines))
    return "\n".join(parts)


async def judge_findings(
    findings: list[tuple[FindingSchema, int]],
    *,
    evidence: dict[int, list[EvidenceRow]],
    verdicts: dict[int, tuple[bool, float, str]],
    agreement: dict[int, list[str]],
    agents: list[str] | None = None,
    repository_block: str = "",
    already_posted_keys: set[str],
) -> tuple[list[Judgment], list[CallRecord]]:
    """One call, whole review. Raises JudgeError if no verdict can be reached."""
    settings = get_settings()
    if not findings:
        return [], []

    rendered = _render_for_judge(findings, evidence, verdicts, agreement, agents)
    calls: list[CallRecord] = []
    verdict = await _call_judge(repository_block, rendered, calls)

    if verdict is None:
        raise JudgeError("judge call produced no verdict")

    judgments = _enforce(
        verdict.judgments,
        findings=findings,
        evidence=evidence,
        verdicts=verdicts,
        already_posted_keys=already_posted_keys,
        settings=settings,
    )
    accepted = sum(1 for j in judgments if j.accepted)
    breakdown: dict[str, int] = {}
    for judgment in judgments:
        if not judgment.accepted:
            breakdown[judgment.rejection_code or "unspecified"] = (
                breakdown.get(judgment.rejection_code or "unspecified", 0) + 1
            )
    logger.info(
        "judged %d findings: %d accepted, rejected %s",
        len(judgments),
        accepted,
        ", ".join(f"{k}={v}" for k, v in sorted(breakdown.items())) or "none",
    )
    if findings and accepted > len(findings) * 0.5:
        # Not an error, but the documented failure mode that makes this phase decorative.
        logger.warning(
            "judge accepted %d of %d candidates — prompt may be too permissive",
            accepted,
            len(findings),
        )
    return judgments, calls


def _enforce(
    judgments: list[Judgment],
    *,
    findings: list[tuple[FindingSchema, int]],
    evidence: dict[int, list[EvidenceRow]],
    verdicts: dict[int, tuple[bool, float, str]],
    already_posted_keys: set[str],
    settings: object,
) -> list[Judgment]:
    """Deterministic rules that override the judge — in the rejecting direction only.

    Each exists because the judge is an LLM and the cost of it being wrong is asymmetric:
    a wrongly rejected finding is invisible, a wrongly accepted one is a developer losing
    trust in the tool.
    """
    threshold: float = settings.CONFIDENCE_THRESHOLD  # type: ignore[attr-defined]
    margin: float = settings.UNVERIFIED_CONFIDENCE_MARGIN  # type: ignore[attr-defined]
    min_severity = SEVERITY_ORDER[settings.MIN_SEVERITY_TO_POST]  # type: ignore[attr-defined]

    by_index = {j.finding_index: j for j in judgments if 0 <= j.finding_index < len(findings)}
    out: list[Judgment] = []

    for index, (finding, _position) in enumerate(findings):
        judgment = by_index.get(index)
        if judgment is None:
            # Rejected by default, never accepted by omission.
            out.append(
                Judgment(
                    finding_index=index,
                    accepted=False,
                    severity=finding.severity,
                    confidence=finding.confidence,
                    reason="Not judged; rejected by default.",
                    rejection_code="weak_evidence",
                )
            )
            continue

        rows = evidence.get(index, [])
        verified, adjusted, _ = verdicts.get(index, (False, finding.confidence, ""))

        if judgment.accepted and has_strong_refutation(rows):
            # The judge disagreed with a high-weight refutation. That is a prompt bug and
            # worth knowing about, so it is logged rather than silently corrected.
            logger.warning(
                "judge accepted a refuted finding (%s:%d); overriding",
                finding.file,
                finding.line,
            )
            judgment.accepted = False
            judgment.rejection_code = "false_positive"
            judgment.reason = "Refuted by high-weight evidence; judge overridden."

        if judgment.accepted and dedupe_key(finding) in already_posted_keys:
            judgment.accepted = False
            judgment.rejection_code = "duplicate"
            judgment.reason = "Already posted on an earlier commit of this pull request."

        # The judge's confidence is capped by the evidence-adjusted one: it may lower a
        # number the evidence supports, never raise one the evidence does not.
        judgment.confidence = min(judgment.confidence, adjusted)

        if judgment.accepted and judgment.confidence < threshold:
            judgment.accepted = False
            judgment.rejection_code = "below_threshold"
            judgment.reason = f"Confidence {judgment.confidence:.2f} below {threshold:.2f}."

        if judgment.accepted and not verified and judgment.confidence < threshold + margin:
            # Unverified findings are held to a higher bar, and must also be severe. A
            # MEDIUM unverified finding is not worth the trust it risks.
            judgment.accepted = False
            judgment.rejection_code = "weak_evidence"
            judgment.reason = (
                f"Unverified and confidence {judgment.confidence:.2f} below the "
                f"unverified bar of {threshold + margin:.2f}."
            )

        if judgment.accepted and not verified and SEVERITY_ORDER[judgment.severity] > SEVERITY_ORDER["HIGH"]:
            judgment.accepted = False
            judgment.rejection_code = "weak_evidence"
            judgment.reason = "Unverified findings below HIGH severity are not posted."

        if judgment.accepted and SEVERITY_ORDER[judgment.severity] > min_severity:
            judgment.accepted = False
            judgment.rejection_code = "below_threshold"
            judgment.reason = f"Severity {judgment.severity} below the posting floor."

        out.append(judgment)

    return out


async def _call_judge(
    repository_block: str, rendered: str, calls: list[CallRecord]
) -> JudgeVerdict | None:
    """The most consequential single call in the pipeline. High effort, no economising."""
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
                contents=[repository_block, rendered],
                config=types.GenerateContentConfig(
                    system_instruction=JUDGE_PROMPT,
                    max_output_tokens=settings.LLM_MAX_TOKENS,
                    response_mime_type="application/json",
                    response_schema=JudgeVerdict,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                    thinking_config=types.ThinkingConfig(
                        thinking_budget=24576 if "flash" not in settings.LLM_MODEL else 16384
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
                        {
                            "type": "text",
                            "text": JUDGE_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    thinking={"type": "adaptive"},
                    output_format=JudgeVerdict,
                    output_config={"effort": "high"},
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": repository_block,
                                    "cache_control": {"type": "ephemeral"},
                                },
                                {"type": "text", "text": rendered},
                            ],
                        }
                    ],
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
    except Exception as exc:
        logger.exception("judge call failed")
        calls.append(
            CallRecord(
                stage="judge",
                provider=settings.LLM_PROVIDER,
                model=settings.LLM_MODEL,
                duration_ms=int((time.perf_counter() - started) * 1000),
                error=str(exc)[:500],
            )
        )
        return None

    calls.append(
        CallRecord(
            stage="judge",
            provider=provider,
            model=settings.LLM_MODEL,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cost_usd=cost,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
    )
    return parsed if isinstance(parsed, JudgeVerdict) else None

"""The LLM reviewer.

One call, structured output, behind a provider switch. `review_diff` is the only entry
point the worker knows about; which vendor answers it is a configuration detail.

The provider split exists because the roadmap's Phase 14 gateway needs it eventually,
and because being able to fall back to a second vendor mid-development is worth more
than the ~40 lines it costs. Neither implementation is commented out — a commented-out
provider rots silently, whereas both of these are type-checked on every CI run.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from agent.prompts import load_agent_section, load_shared
from agent.schemas import CallRecord, FindingsReport, ReviewResult, ReviewUsage
from agent.tools.gateway import ToolGateway
from settings import get_settings

logger = logging.getLogger(__name__)

# USD per million tokens, (input, output). Anthropic cache writes bill at ~1.25x input
# and reads at ~0.1x; Gemini's implicit cache reads bill at ~0.25x and it has no
# separate write charge.
#
# NOTE: verify the Gemini rates against ai.google.dev/pricing before trusting a cost
# report. They are here so cost_usd is not silently zero, not because they are audited.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
}

_DEFAULT_PRICE = (5.00, 25.00)

# LLM_EFFORT is Anthropic's vocabulary. Gemini 2.5 expresses the same idea as a token
# allowance for reasoning. (`thinking_level` exists in the SDK but the 2.5 models reject
# it with 400 INVALID_ARGUMENT — it is for later model generations.)
_GEMINI_THINKING_BUDGET: dict[str, int] = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "xhigh": 24576,
    "max": 32768,
}

# Pro accepts up to 32768; Flash caps at 24576 and 400s above it.
_GEMINI_FLASH_MAX_THINKING = 24576


def _gemini_thinking_budget(model: str, effort: str) -> int:
    budget = _GEMINI_THINKING_BUDGET[effort]
    if "flash" in model:
        return min(budget, _GEMINI_FLASH_MAX_THINKING)
    return budget

# finish_reason values that mean "the model declined", not "the model answered".
_GEMINI_REFUSAL_REASONS = frozenset(
    {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}
)


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_creation: int,
    *,
    cache_read_multiplier: float = 0.10,
) -> float:
    input_price, output_price = PRICING.get(model, _DEFAULT_PRICE)
    total = (
        input_tokens * input_price
        + output_tokens * output_price
        + cache_creation * input_price * 1.25
        + cache_read * input_price * cache_read_multiplier
    )
    return total / 1_000_000


def render_focus(agent: str, focus: list[str] | None) -> str:
    """The planner's per-agent file hints, as an uncached trailing block.

    **This must never enter the cached prefix.** The repository bundle behind the cache
    breakpoint is byte-identical across all three agents, which is what makes agents two
    and three cost ~0.1x for it. Interpolating an agent name or a focus list into that
    block gives each agent a different prefix, every cache read becomes a cache write,
    and the phase costs triple instead of a third more.
    """
    if not focus:
        return ""
    listed = "\n".join(f"- {path}" for path in focus[:10])
    return f"<focus agent=\"{agent}\">\nLook at these files first:\n{listed}\n</focus>"


async def review_diff(
    repository_block: str,
    diff_block: str,
    *,
    gateway: ToolGateway | None = None,
    agent: str = "reviewer",
    focus: list[str] | None = None,
) -> ReviewResult:
    """Investigate (if tools are available), then report.

    Without a gateway this is the Phase 3 behaviour: one call over whatever context was
    assembled up front. With one, the agent explores the repository first and the
    reporting call sees what it found — which is the difference between a reviewer that
    pattern-matches and one that checks.
    """
    settings = get_settings()
    # Identical across agents, so the cached prefix runs from here through the diff.
    system_prompt = load_shared()
    role_block = f"<your_role>\n{load_agent_section(agent)}\n</your_role>"

    investigation = None
    if gateway is not None:
        from agent.investigate import investigate

        investigation = await investigate(
            gateway=gateway,
            system_prompt=system_prompt,
            repository_block=repository_block,
            diff_block=diff_block,
            role_block=role_block,
            agent=agent,
        )

    # Everything that varies per agent rides here, after the shared prefix: the role, the
    # planner's focus hints, and the investigation transcript.
    tail = "\n".join(
        t
        for t in (
            role_block,
            render_focus(agent, focus),
            investigation.transcript if investigation else "",
        )
        if t
    )
    if settings.LLM_PROVIDER == "gemini":
        result = await _review_gemini(repository_block, diff_block, tail, agent)
    else:
        result = await _review_anthropic(repository_block, diff_block, tail, agent)

    if investigation is not None:
        # Investigation calls come first so `llm_calls` reads in execution order.
        result.calls = [*investigation.calls, *result.calls]
        result.tool_iterations = investigation.iterations
        result.budget_stopped = investigation.stopped_by
        result.usage.input_tokens += sum(c.input_tokens for c in investigation.calls)
        result.usage.output_tokens += sum(c.output_tokens for c in investigation.calls)
        result.usage.cache_read_tokens += sum(c.cache_read_tokens for c in investigation.calls)
        result.usage.cost_usd += investigation.cost_usd

    return result


async def _review_anthropic(
    repository_block: str, diff_block: str, transcript: str = "", agent: str = "reviewer"
) -> ReviewResult:
    """Claude via the Anthropic SDK.

    The prompt-cache layout is deliberate and set up now because it is expensive to
    retrofit: the system prompt and the repository block each get a breakpoint, so when
    Phase 6 fans out to three agents they share the repository context at ~0.1x input
    cost instead of each paying full price.
    """
    from anthropic import AsyncAnthropic

    settings = get_settings()
    client = AsyncAnthropic(
        api_key=settings.ANTHROPIC_API_KEY,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=settings.LLM_MAX_RETRIES,
    )

    tail: list[Any] = [{"type": "text", "text": transcript}] if transcript else []

    started = time.perf_counter()
    try:
        response = await client.messages.parse(
            model=settings.LLM_MODEL,
            max_tokens=settings.LLM_MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": load_shared(),
                    # Breakpoint 1: frozen across every review of every repository.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            thinking={"type": "adaptive"},
            output_format=FindingsReport,
            output_config={"effort": settings.LLM_EFFORT},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": repository_block,
                            # Breakpoint 2: shared by every agent in this review.
                            "cache_control": {"type": "ephemeral"},
                        },
                        # Uncached tail — varies per call, so it must come last.
                        # The investigation transcript belongs here too: it differs on
                        # every review and would poison the cached prefix.
                        {"type": "text", "text": diff_block},
                        *tail,
                    ],
                }
            ],
        )
    finally:
        await client.close()

    usage = ReviewUsage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_read_tokens=response.usage.cache_read_input_tokens or 0,
        cache_creation_tokens=response.usage.cache_creation_input_tokens or 0,
        stop_reason=response.stop_reason,
    )
    usage.cost_usd = estimate_cost(
        settings.LLM_MODEL,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_creation_tokens,
    )
    record = _record("anthropic", settings.LLM_MODEL, usage, started)

    # Check stop_reason before touching output. A refusal is HTTP 200 with empty or
    # partial content, so reading parsed_output first would raise in production and
    # never in development.
    if response.stop_reason == "refusal":
        logger.warning("reviewer refused", extra={"details": str(response.stop_details)})
        return ReviewResult(
            report=FindingsReport(findings=[]), usage=usage, refused=True, calls=[record]
        )

    if response.stop_reason == "max_tokens":
        logger.warning("reviewer hit max_tokens; findings may be truncated")

    report = response.parsed_output or FindingsReport(findings=[])
    _log_complete(report, usage)
    return ReviewResult(report=report, usage=usage, calls=[record])


async def _review_gemini(
    repository_block: str, diff_block: str, transcript: str = "", agent: str = "reviewer"
) -> ReviewResult:
    """Gemini via google-genai.

    No explicit cache breakpoints: Gemini caches repeated prefixes implicitly, so the
    repository block benefits automatically without the two-breakpoint layout the
    Anthropic path needs. Ordering still matters — the stable block goes first.
    """
    from google import genai
    from google.genai import types

    settings = get_settings()
    client = genai.Client(
        api_key=settings.GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=int(settings.LLM_TIMEOUT_SECONDS * 1000)),
    )

    started = time.perf_counter()
    response = await client.aio.models.generate_content(
        model=settings.LLM_MODEL,
        contents=[repository_block, diff_block, *([transcript] if transcript else [])],
        config=types.GenerateContentConfig(
            system_instruction=load_shared(),
            max_output_tokens=settings.LLM_MAX_TOKENS,
            response_mime_type="application/json",
            # A Pydantic model is accepted directly, so the schema stays single-sourced
            # with the Anthropic path rather than being hand-written as JSON Schema.
            response_schema=FindingsReport,
            # No tools are declared here, so AFC has nothing to invoke — but the SDK
            # logs that it is enabled regardless, and that line arriving right after
            # "investigation complete" reads as though the tool loop used AFC.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(
                thinking_budget=_gemini_thinking_budget(settings.LLM_MODEL, settings.LLM_EFFORT),
            ),
        ),
    )

    meta = response.usage_metadata
    candidates = response.candidates or []
    finish_reason = candidates[0].finish_reason if candidates else None
    finish_name = getattr(finish_reason, "name", None) or str(finish_reason or "")

    usage = ReviewUsage(
        input_tokens=(meta.prompt_token_count or 0) if meta else 0,
        # Thinking tokens bill as output but are reported separately.
        output_tokens=(
            ((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)) if meta else 0
        ),
        cache_read_tokens=(meta.cached_content_token_count or 0) if meta else 0,
        # Gemini's implicit cache has no separate write charge to attribute.
        cache_creation_tokens=0,
        stop_reason=finish_name,
    )
    usage.cost_usd = estimate_cost(
        settings.LLM_MODEL,
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_creation_tokens,
        cache_read_multiplier=0.25,
    )
    record = _record("gemini", settings.LLM_MODEL, usage, started)

    # Same ordering rule as the Anthropic path: a blocked response is a 200 with no
    # usable content, so the finish reason is checked before `parsed` is touched.
    if finish_name in _GEMINI_REFUSAL_REASONS:
        logger.warning("reviewer refused", extra={"finish_reason": finish_name})
        return ReviewResult(
            report=FindingsReport(findings=[]), usage=usage, refused=True, calls=[record]
        )

    if finish_name == "MAX_TOKENS":
        logger.warning("reviewer hit max_tokens; findings may be truncated")

    parsed = response.parsed
    report = parsed if isinstance(parsed, FindingsReport) else FindingsReport(findings=[])
    if parsed is not None and not isinstance(parsed, FindingsReport):
        logger.warning("unexpected parsed type %s; treating as empty", type(parsed).__name__)
    _log_complete(report, usage)
    return ReviewResult(report=report, usage=usage, calls=[record])


def _record(provider: str, model: str, usage: ReviewUsage, started: float) -> CallRecord:
    """The reporting call, as an `llm_calls` row."""
    return CallRecord(
        stage="report",
        provider=provider,
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_creation_tokens=usage.cache_creation_tokens,
        cost_usd=usage.cost_usd,
        stop_reason=usage.stop_reason,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def _log_complete(report: FindingsReport, usage: ReviewUsage) -> None:
    logger.info(
        "review complete: %d findings, $%.4f, %d cached input tokens (%s)",
        len(report.findings),
        usage.cost_usd,
        usage.cache_read_tokens,
        get_settings().LLM_PROVIDER,
    )

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
from pathlib import Path

from agent.schemas import FindingsReport, ReviewResult, ReviewUsage
from settings import get_settings

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "code_review.md"

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

# LLM_EFFORT is Anthropic's vocabulary. Gemini expresses the same idea as a coarser
# ThinkingLevel, so the top three collapse to HIGH.
_GEMINI_THINKING_LEVEL: dict[str, str] = {
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "xhigh": "HIGH",
    "max": "HIGH",
}

# finish_reason values that mean "the model declined", not "the model answered".
_GEMINI_REFUSAL_REASONS = frozenset(
    {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}
)


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


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


async def review_diff(repository_block: str, diff_block: str) -> ReviewResult:
    """Run one review pass and return validated findings plus usage."""
    provider = get_settings().LLM_PROVIDER
    if provider == "gemini":
        return await _review_gemini(repository_block, diff_block)
    return await _review_anthropic(repository_block, diff_block)


async def _review_anthropic(repository_block: str, diff_block: str) -> ReviewResult:
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

    try:
        response = await client.messages.parse(
            model=settings.LLM_MODEL,
            max_tokens=settings.LLM_MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": load_prompt(),
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
                        {"type": "text", "text": diff_block},
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

    # Check stop_reason before touching output. A refusal is HTTP 200 with empty or
    # partial content, so reading parsed_output first would raise in production and
    # never in development.
    if response.stop_reason == "refusal":
        logger.warning("reviewer refused", extra={"details": str(response.stop_details)})
        return ReviewResult(report=FindingsReport(findings=[]), usage=usage, refused=True)

    if response.stop_reason == "max_tokens":
        logger.warning("reviewer hit max_tokens; findings may be truncated")

    report = response.parsed_output or FindingsReport(findings=[])
    _log_complete(report, usage)
    return ReviewResult(report=report, usage=usage)


async def _review_gemini(repository_block: str, diff_block: str) -> ReviewResult:
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

    response = await client.aio.models.generate_content(
        model=settings.LLM_MODEL,
        contents=[repository_block, diff_block],
        config=types.GenerateContentConfig(
            system_instruction=load_prompt(),
            max_output_tokens=settings.LLM_MAX_TOKENS,
            response_mime_type="application/json",
            # A Pydantic model is accepted directly, so the schema stays single-sourced
            # with the Anthropic path rather than being hand-written as JSON Schema.
            response_schema=FindingsReport,
            thinking_config=types.ThinkingConfig(
                thinking_level=_GEMINI_THINKING_LEVEL[settings.LLM_EFFORT],
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

    # Same ordering rule as the Anthropic path: a blocked response is a 200 with no
    # usable content, so the finish reason is checked before `parsed` is touched.
    if finish_name in _GEMINI_REFUSAL_REASONS:
        logger.warning("reviewer refused", extra={"finish_reason": finish_name})
        return ReviewResult(report=FindingsReport(findings=[]), usage=usage, refused=True)

    if finish_name == "MAX_TOKENS":
        logger.warning("reviewer hit max_tokens; findings may be truncated")

    parsed = response.parsed
    report = parsed if isinstance(parsed, FindingsReport) else FindingsReport(findings=[])
    if parsed is not None and not isinstance(parsed, FindingsReport):
        logger.warning("unexpected parsed type %s; treating as empty", type(parsed).__name__)
    _log_complete(report, usage)
    return ReviewResult(report=report, usage=usage)


def _log_complete(report: FindingsReport, usage: ReviewUsage) -> None:
    logger.info(
        "review complete",
        extra={
            "provider": get_settings().LLM_PROVIDER,
            "findings": len(report.findings),
            "cost_usd": round(usage.cost_usd, 4),
            "cache_read": usage.cache_read_tokens,
        },
    )

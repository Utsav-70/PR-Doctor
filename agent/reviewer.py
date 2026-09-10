"""The LLM reviewer.

One call, structured output. The prompt-cache layout is deliberate and set up now
because it is expensive to retrofit: the system prompt and the repository block each
get a breakpoint, so when Phase 6 fans out to three agents they share the repository
context at ~0.1x input cost instead of each paying full price.
"""

from __future__ import annotations

import logging
from pathlib import Path

from anthropic import AsyncAnthropic

from agent.schemas import FindingsReport, ReviewResult, ReviewUsage
from settings import get_settings

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "code_review.md"

# USD per million tokens. Cache writes bill at ~1.25x input, reads at ~0.1x.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int,
    cache_creation: int,
) -> float:
    input_price, output_price = PRICING.get(model, PRICING["claude-opus-5"])
    total = (
        input_tokens * input_price
        + output_tokens * output_price
        + cache_creation * input_price * 1.25
        + cache_read * input_price * 0.10
    )
    return total / 1_000_000


async def review_diff(repository_block: str, diff_block: str) -> ReviewResult:
    """Run one review pass and return validated findings plus usage."""
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
    logger.info(
        "review complete",
        extra={
            "findings": len(report.findings),
            "cost_usd": round(usage.cost_usd, 4),
            "cache_read": usage.cache_read_tokens,
        },
    )
    return ReviewResult(report=report, usage=usage)

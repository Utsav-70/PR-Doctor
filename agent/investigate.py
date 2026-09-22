"""The tool loop — Phase 5's investigation stage.

Two phases per review, and the split is forced rather than chosen:

    1. investigate   tools enabled, free-form output. The agent reads definitions,
                     finds call sites, checks tests.
    2. report        structured output, no tools. The findings, against the schema.

Neither provider allows function declarations and a response schema in the same
request, so a single call that both investigates and returns validated findings is not
available. Splitting it also happens to be better: the investigation transcript becomes
explicit input to the reporting call, which is exactly the evidence Phase 7 will verify.

The loop is hand-written on both providers rather than using the SDK helpers
(Anthropic's tool runner, Gemini's AFC). Those execute tool callables directly, which
bypasses the Phase 4 gateway — and with it every permission check, rate limit, and
`tool_calls` audit row. The gateway being the single dispatch point is the whole reason
it exists.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from agent.schemas import CallRecord
from agent.tools.gateway import ToolGateway
from settings import get_settings

logger = logging.getLogger(__name__)


class Investigation:
    """Accumulated findings-in-progress from the tool phase."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.calls: list[CallRecord] = []
        self.iterations: int = 0
        self.stopped_by: str | None = None

    @property
    def transcript(self) -> str:
        """The investigation as prompt input for the reporting call."""
        if not self.notes:
            return ""
        body = "\n".join(self.notes)
        return f"<investigation>\n{body}\n</investigation>"

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)


# Appended to the review prompt for the investigation phase only.
#
# Without it the agent called no tools at all: the review prompt says "surrounding code
# is provided so you can understand what the change does", which reads as *everything
# you need is already here*. The model wrote its findings in the investigation call,
# the reporting call then wrote them again, and the tool budget went unspent. The
# investigation phase needs a different job description from the reporting phase.
INVESTIGATION_DIRECTIVE = """

## This request is the investigation phase, not the report

The context above is a starting point, not the whole repository. It was assembled by a
script that guessed what you would need, and on a large pull request it reaches only
some of the files.

Use the tools before judging the change. In particular:

- For anything the diff **calls**, read its definition — do not assume what it returns,
  what it raises, or what units its arguments are in.
- For anything the diff **defines or changes the signature of**, find its callers. A
  change that breaks a caller is invisible in the diff, and it is the most valuable
  thing you can find here.
- Check whether the changed behaviour is tested.
- Verify anything you are about to report at low confidence. A lookup that confirms or
  kills a suspicion is worth more than a hedge.

Do not produce findings yet, and do not write descriptions for the developer — a second
request does that, and it will see everything you write here. Report only what you
looked up and what it told you, including the lookups that came back clean.

Stop calling tools once further lookups would not change your judgement of the change.
"""


def _render_tool_result(tool: str, args: dict[str, Any], payload: str) -> str:
    """Tool output as text for the transcript.

    Rendered rather than passed as structured tool-result blocks because the reporting
    call is a separate request with no tool context — it needs to read what happened,
    not replay it.
    """
    compact = json.dumps(json.loads(payload), sort_keys=True)[:4000]
    return f"<tool_call name=\"{tool}\" args={json.dumps(args, sort_keys=True)}>\n{compact}\n</tool_call>"


async def investigate_gemini(
    *,
    gateway: ToolGateway,
    system_prompt: str,
    repository_block: str,
    diff_block: str,
    agent: str = "reviewer",
) -> Investigation:
    """Let Gemini explore the repository before reporting.

    AFC is disabled explicitly. Left on, the SDK would execute tool callables itself,
    cap the loop at its own 10 round trips, and produce no audit rows — see the module
    docstring.
    """
    from google import genai
    from google.genai import types

    settings = get_settings()
    state = Investigation()
    client = genai.Client(
        api_key=settings.GEMINI_API_KEY,
        http_options=types.HttpOptions(timeout=int(settings.LLM_TIMEOUT_SECONDS * 1000)),
    )

    declarations = [
        types.FunctionDeclaration(
            name=d["name"], description=d["description"], parameters_json_schema=d["input_schema"]
        )
        for d in gateway.tool_definitions()
    ]
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[types.Tool(function_declarations=declarations)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        max_output_tokens=settings.LLM_MAX_TOKENS,
    )

    contents: list[types.Content] = [
        types.Content(
            role="user",
            parts=[
                types.Part(text=repository_block),
                types.Part(text=diff_block),
                types.Part(
                    text=(
                        "Investigate this change using the tools before reporting. Look up "
                        "definitions of what it calls, find callers of what it defines, and "
                        "check whether tests cover it. When you have enough to judge the "
                        "change, stop calling tools and summarise what you found."
                    )
                ),
            ],
        )
    ]

    for iteration in range(settings.MAX_TOOL_ITERATIONS):
        if state.cost_usd >= settings.MAX_COST_PER_REVIEW_USD:
            state.stopped_by = "cost"
            break
        if len(state.calls) >= settings.MAX_LLM_CALLS_PER_REVIEW:
            state.stopped_by = "llm_calls"
            break

        started = time.perf_counter()
        response = await client.aio.models.generate_content(
            model=settings.LLM_MODEL, contents=contents, config=config
        )
        state.calls.append(_gemini_record(response, settings, started, iteration))
        state.iterations = iteration + 1

        candidates = response.candidates or []
        parts = list(candidates[0].content.parts or []) if candidates and candidates[0].content else []
        requested = [p.function_call for p in parts if p.function_call]

        for part in parts:
            if part.text:
                state.notes.append(part.text.strip())

        if not requested:
            break

        contents.append(types.Content(role="model", parts=parts))
        response_parts: list[types.Part] = []
        for call in requested:
            args = dict(call.args or {})
            result = await gateway.call(agent, call.name or "", args)
            payload = result.model_dump_json()
            state.notes.append(_render_tool_result(call.name or "", args, payload))
            response_parts.append(
                types.Part(
                    function_response=types.FunctionResponse(
                        name=call.name, response={"result": payload[:8000]}
                    )
                )
            )
        contents.append(types.Content(role="user", parts=response_parts))
    else:
        # Loop ran to the cap without the agent volunteering to stop.
        state.stopped_by = "iterations"

    logger.info(
        "investigation complete",
        extra={
            "iterations": state.iterations,
            "tool_calls": gateway.usage.calls,
            "denied": gateway.usage.denied,
            "cost_usd": round(state.cost_usd, 4),
            "stopped_by": state.stopped_by,
        },
    )
    return state


def _gemini_record(response: Any, settings: Any, started: float, iteration: int) -> CallRecord:
    from agent.reviewer import estimate_cost

    meta = response.usage_metadata
    candidates = response.candidates or []
    finish = candidates[0].finish_reason if candidates else None
    input_tokens = (meta.prompt_token_count or 0) if meta else 0
    output_tokens = (
        ((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)) if meta else 0
    )
    cache_read = (meta.cached_content_token_count or 0) if meta else 0
    return CallRecord(
        stage="investigate",
        provider="gemini",
        model=settings.LLM_MODEL,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cost_usd=estimate_cost(
            settings.LLM_MODEL,
            input_tokens,
            output_tokens,
            cache_read,
            0,
            cache_read_multiplier=0.25,
        ),
        stop_reason=getattr(finish, "name", None),
        duration_ms=int((time.perf_counter() - started) * 1000),
        tool_iterations=iteration + 1,
    )


async def investigate_anthropic(
    *,
    gateway: ToolGateway,
    system_prompt: str,
    repository_block: str,
    diff_block: str,
    agent: str = "reviewer",
) -> Investigation:
    """Same loop against Claude.

    The tool runner would be less code, but it executes tool callables itself and would
    route around the gateway. Keeping both providers on one hand-written loop also means
    the investigation transcript has one shape regardless of who produced it.
    """
    from anthropic import AsyncAnthropic

    from agent.reviewer import estimate_cost

    settings = get_settings()
    state = Investigation()
    client = AsyncAnthropic(
        api_key=settings.ANTHROPIC_API_KEY,
        timeout=settings.LLM_TIMEOUT_SECONDS,
        max_retries=settings.LLM_MAX_RETRIES,
    )

    # The SDK's TypedDicts are stricter than what we build here; the shapes are correct
    # at runtime and the registry is the single source of truth for them.
    tools: Any = [
        {"name": d["name"], "description": d["description"], "input_schema": d["input_schema"]}
        for d in gateway.tool_definitions()
    ]
    messages: list[Any] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": repository_block, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": diff_block},
                {
                    "type": "text",
                    "text": (
                        "Investigate this change using the tools before reporting. When you "
                        "have enough to judge it, stop calling tools and summarise."
                    ),
                },
            ],
        }
    ]

    try:
        for iteration in range(settings.MAX_TOOL_ITERATIONS):
            if state.cost_usd >= settings.MAX_COST_PER_REVIEW_USD:
                state.stopped_by = "cost"
                break
            if len(state.calls) >= settings.MAX_LLM_CALLS_PER_REVIEW:
                state.stopped_by = "llm_calls"
                break

            started = time.perf_counter()
            response = await client.messages.create(
                model=settings.LLM_MODEL,
                max_tokens=settings.LLM_MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=tools,
                messages=messages,
            )
            usage = response.usage
            state.calls.append(
                CallRecord(
                    stage="investigate",
                    provider="anthropic",
                    model=settings.LLM_MODEL,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_input_tokens or 0,
                    cache_creation_tokens=usage.cache_creation_input_tokens or 0,
                    cost_usd=estimate_cost(
                        settings.LLM_MODEL,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.cache_read_input_tokens or 0,
                        usage.cache_creation_input_tokens or 0,
                    ),
                    stop_reason=response.stop_reason,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                    tool_iterations=iteration + 1,
                )
            )
            state.iterations = iteration + 1

            for block in response.content:
                if block.type == "text":
                    state.notes.append(block.text.strip())

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                break

            messages.append({"role": "assistant", "content": response.content})
            results: list[dict[str, Any]] = []
            for block in tool_uses:
                args = dict(block.input) if isinstance(block.input, dict) else {}
                result = await gateway.call(agent, block.name, args)
                payload = result.model_dump_json()
                state.notes.append(_render_tool_result(block.name, args, payload))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": payload[:8000],
                        "is_error": not result.ok,
                    }
                )
            messages.append({"role": "user", "content": results})
        else:
            state.stopped_by = "iterations"
    finally:
        await client.close()

    logger.info(
        "investigation complete",
        extra={
            "iterations": state.iterations,
            "tool_calls": gateway.usage.calls,
            "cost_usd": round(state.cost_usd, 4),
            "stopped_by": state.stopped_by,
        },
    )
    return state


async def investigate(
    *,
    gateway: ToolGateway,
    system_prompt: str,
    repository_block: str,
    diff_block: str,
    agent: str = "reviewer",
) -> Investigation:
    system_prompt = system_prompt + INVESTIGATION_DIRECTIVE
    if get_settings().LLM_PROVIDER == "gemini":
        return await investigate_gemini(
            gateway=gateway,
            system_prompt=system_prompt,
            repository_block=repository_block,
            diff_block=diff_block,
            agent=agent,
        )
    return await investigate_anthropic(
        gateway=gateway,
        system_prompt=system_prompt,
        repository_block=repository_block,
        diff_block=diff_block,
        agent=agent,
    )

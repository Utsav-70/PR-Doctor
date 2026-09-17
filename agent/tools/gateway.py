"""The tool gateway — the single dispatch point, and the reason these are a layer
rather than five helper functions.

Everything an agent does to a repository passes through `call()`, which means one
place enforces permissions, one place counts the budget, and one place writes the
audit row. A helper function called directly from three agents has none of that, and
retrofitting it once Phase 11 can execute code is the wrong time to find out.

Errors are values throughout. A denied permission, a bad argument, a missing file — all
return a `ToolResult` the agent can read and adapt to. Nothing here raises into the
review.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from agent.tools.read_file import read_file
from agent.tools.results import ToolResult
from agent.tools.search import search_code
from agent.tools.symbols import find_references, get_symbol
from agent.tools.tests import get_tests
from agent.tools.workspace import Workspace

logger = logging.getLogger(__name__)


# --- argument schemas ---------------------------------------------------------------
# Validated before dispatch so a malformed call returns a typed error the agent can
# recover from, instead of a TypeError that kills the review.


class ReadFileArgs(BaseModel):
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class SearchCodeArgs(BaseModel):
    pattern: str
    path_glob: str | None = None
    max_results: int = Field(default=50, ge=1, le=200)


class GetSymbolArgs(BaseModel):
    name: str
    kind: str | None = None
    include_body: bool = False
    max_results: int = Field(default=20, ge=1, le=100)


class FindReferencesArgs(BaseModel):
    name: str
    max_results: int = Field(default=50, ge=1, le=200)


class GetTestsArgs(BaseModel):
    symbol: str | None = None
    path: str | None = None
    max_results: int = Field(default=40, ge=1, le=100)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    args_model: type[BaseModel]
    handler: Callable[..., Any]
    is_async: bool
    description: str


REGISTRY: dict[str, ToolSpec] = {
    "read_file": ToolSpec(
        "read_file", ReadFileArgs, read_file, False,
        "Read a file, or a line range, with line numbers attached.",
    ),
    "search_code": ToolSpec(
        "search_code", SearchCodeArgs, search_code, True,
        "Regex search across the repository, with surrounding context lines.",
    ),
    "get_symbol": ToolSpec(
        "get_symbol", GetSymbolArgs, get_symbol, False,
        "Find where a function, class, method, or import is defined.",
    ),
    "find_references": ToolSpec(
        "find_references", FindReferencesArgs, find_references, True,
        "Find genuine uses of a symbol, excluding comments and strings.",
    ),
    "get_tests": ToolSpec(
        "get_tests", GetTestsArgs, get_tests, True,
        "Find tests related to a changed symbol or file.",
    ),
}

# Per-agent allowlists. The performance agent has no business reading arbitrary files;
# the security agent has no business enumerating tests. Phase 6 adds the other agents;
# `reviewer` is the single agent Phase 5 runs.
DEFAULT_PERMISSIONS: dict[str, frozenset[str]] = {
    "reviewer": frozenset(REGISTRY),
    "bug": frozenset(REGISTRY),
    "security": frozenset({"read_file", "search_code", "get_symbol", "find_references"}),
    "performance": frozenset({"read_file", "get_symbol", "find_references"}),
    "judge": frozenset({"read_file", "get_symbol"}),
}

MAX_TOOL_CALLS_PER_REVIEW = 60
MAX_CALLS_PER_TOOL = 25
MAX_RESULT_TOKENS = 120_000


def estimate_tokens(text: str) -> int:
    """Rough token count for budgeting.

    KNOWN GAP (Phase 4 exit criterion): the spec calls for the API's `count_tokens`
    rather than a character heuristic. That is one network round trip per tool result,
    which is not obviously worth it inside a tool loop — the decision is deferred to
    Phase 14, where the LLM gateway centralises token accounting anyway. Until then
    this over-estimates slightly on code, which fails safe.
    """
    return max(len(text) // 4, 1)


@dataclass
class GatewayUsage:
    calls: int = 0
    per_tool: dict[str, int] = field(default_factory=dict)
    result_tokens: int = 0
    denied: int = 0


class ToolGateway:
    """Dispatch, permission-check, rate-limit, budget, and audit every tool call."""

    def __init__(
        self,
        workspace: Workspace,
        *,
        review_id: uuid.UUID | None = None,
        permissions: dict[str, frozenset[str]] | None = None,
        max_calls: int = MAX_TOOL_CALLS_PER_REVIEW,
        max_calls_per_tool: int = MAX_CALLS_PER_TOOL,
        max_result_tokens: int = MAX_RESULT_TOKENS,
        audit: bool = True,
    ) -> None:
        self.workspace = workspace
        self.review_id = review_id
        self.permissions = permissions or DEFAULT_PERMISSIONS
        self.max_calls = max_calls
        self.max_calls_per_tool = max_calls_per_tool
        self.max_result_tokens = max_result_tokens
        self.audit = audit and review_id is not None
        self.usage = GatewayUsage()

    def tool_definitions(self) -> list[dict[str, Any]]:
        """Registry as JSON-Schema tool definitions, for whichever model API is in use."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.args_model.model_json_schema(),
            }
            for spec in REGISTRY.values()
        ]

    async def call(self, agent: str, tool: str, args: dict[str, Any]) -> ToolResult:
        started = time.perf_counter()
        denied = self._deny_reason(agent, tool)
        if denied is not None:
            self.usage.denied += 1
            result = ToolResult.failure(tool, denied)
            await self._record(agent, tool, args, result, started, allowed=False, reason=denied)
            return result

        spec = REGISTRY[tool]
        try:
            validated = spec.args_model(**args)
        except ValidationError as exc:
            message = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            result = ToolResult.failure(tool, f"invalid arguments: {message[:300]}")
            await self._record(agent, tool, args, result, started, allowed=True)
            return result

        self.usage.calls += 1
        self.usage.per_tool[tool] = self.usage.per_tool.get(tool, 0) + 1

        try:
            payload = validated.model_dump(exclude_none=True)
            if spec.is_async:
                result = await spec.handler(self.workspace, **payload)
            else:
                result = spec.handler(self.workspace, **payload)
        except Exception as exc:
            logger.exception("tool raised", extra={"tool": tool, "agent": agent})
            result = ToolResult.failure(tool, f"{type(exc).__name__}: {exc}"[:300])

        result = self._charge_budget(result)
        await self._record(agent, tool, args, result, started, allowed=True)
        return result

    def _deny_reason(self, agent: str, tool: str) -> str | None:
        if tool not in REGISTRY:
            return f"unknown tool: {tool}"
        allowed = self.permissions.get(agent)
        if allowed is None:
            return f"unknown agent: {agent}"
        if tool not in allowed:
            return f"agent {agent!r} is not permitted to call {tool!r}"
        if self.usage.calls >= self.max_calls:
            return f"tool call budget exhausted ({self.max_calls} per review)"
        if self.usage.per_tool.get(tool, 0) >= self.max_calls_per_tool:
            return f"per-tool budget exhausted ({self.max_calls_per_tool} calls to {tool})"
        if self.usage.result_tokens >= self.max_result_tokens:
            return f"result token budget exhausted ({self.max_result_tokens})"
        return None

    def _charge_budget(self, result: ToolResult) -> ToolResult:
        tokens = estimate_tokens(result.model_dump_json())
        self.usage.result_tokens += tokens
        if self.usage.result_tokens > self.max_result_tokens:
            # Report the overrun rather than silently returning a result the caller
            # cannot afford to include.
            result.note = (
                f"{result.note + '; ' if result.note else ''}"
                f"result token budget exceeded ({self.usage.result_tokens}/"
                f"{self.max_result_tokens}); later calls will be denied"
            )
        return result

    async def _record(
        self,
        agent: str,
        tool: str,
        args: dict[str, Any],
        result: ToolResult,
        started: float,
        *,
        allowed: bool,
        reason: str | None = None,
    ) -> None:
        payload = result.model_dump_json()
        duration_ms = int((time.perf_counter() - started) * 1000)

        logger.info(
            "tool call",
            extra={
                "agent": agent,
                "tool": tool,
                "allowed": allowed,
                "ok": result.ok,
                "duration_ms": duration_ms,
                "result_bytes": len(payload),
            },
        )
        if not self.audit or self.review_id is None:
            return

        # Imported here so the tools stay usable without a database — they are pure
        # functions and the sample-repo harness has no Postgres.
        from db.models import ToolCall
        from db.session import session_scope

        async with session_scope() as session:
            session.add(
                ToolCall(
                    review_id=self.review_id,
                    agent=agent[:32],
                    tool=tool[:32],
                    args=_safe_args(args),
                    allowed=allowed,
                    denied_reason=reason[:64] if reason else None,
                    ok=result.ok,
                    error=result.error[:512] if result.error else None,
                    result_bytes=len(payload),
                    result_tokens=estimate_tokens(payload),
                    duration_ms=duration_ms,
                )
            )


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    """Arguments as received, bounded. Recorded even when rejected — a traversal
    attempt is only diagnosable if the offending argument was kept."""
    return {str(k)[:64]: (v[:500] if isinstance(v, str) else v) for k, v in args.items()}

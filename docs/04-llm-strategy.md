# LLM Strategy

Everything model-facing in one place: which models, how output is constrained, how
prompts are cached, and how cost is bounded. Phases 5, 6, 7, 8, and 14 all implement
parts of this document.

Provider: **Anthropic Claude**, via the official `anthropic` Python SDK.

## Models

| Role | Model ID | Context | Input $/MTok | Output $/MTok |
|---|---|---|---|---|
| Default for all reasoning | `claude-opus-5` | 1M | $5.00 | $25.00 |
| High-volume alternative | `claude-sonnet-5` | 1M | $3.00 | $15.00 |
| Mechanical classification only | `claude-haiku-4-5` | 200K | $1.00 | $5.00 |

Use the exact ID strings — they are complete as written. Do **not** append date suffixes.

**`claude-opus-5` is the default for every agent that reasons about code**: the bug,
security, and performance reviewers, the evidence agent, and the judge. Code review is
precisely the workload where model capability converts directly into precision, which is
the metric the whole product is optimised for. Downgrading a reviewer to save money
trades away the thing that makes PRGuard worth running.

Haiku 4.5 is reserved for work with no judgment in it — file-category classification when
extension heuristics are ambiguous, and title normalisation for deduplication. See
[Phase 14](phases/phase-14-cost.md) for the router.

Max output is 128K on Opus 5 and Sonnet 5, 64K on Haiku 4.5.

## Request shape

The canonical reviewer call:

```python
import anthropic

client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY from the environment

response = client.messages.parse(
    model="claude-opus-5",
    max_tokens=16000,
    system=[
        {"type": "text", "text": AGENT_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
    ],
    thinking={"type": "adaptive"},
    output_config={
        "effort": "high",
        "format": FindingsReport,  # Pydantic model — see below
    },
    messages=[
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": repo_context_block,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": diff_block},
            ],
        },
    ],
)
report = response.parsed_output  # a validated FindingsReport
```

Five things in that call are deliberate and load-bearing.

### `thinking={"type": "adaptive"}`

Adaptive thinking lets the model decide how much reasoning each diff warrants. On
`claude-opus-5` thinking is **on by default** — omitting the parameter runs adaptive
anyway — but state it explicitly so the intent survives a future model change.

Two consequences to plan for:

- `max_tokens` caps thinking **plus** response text. A budget sized only for the JSON
  output will truncate mid-answer. 16000 is the floor for a reviewer; raise it before
  raising `effort`.
- Raw chain of thought is never returned. `thinking.display` defaults to `"omitted"`, so
  thinking blocks arrive with empty text. Set `display: "summarized"` when exporting
  reasoning to Langfuse for debugging (Phase 15) — it changes visibility only, never
  billing.

Do **not** disable thinking on `claude-opus-5`. With thinking off, the model
occasionally writes a tool call into its visible text instead of emitting a `tool_use`
block — the turn succeeds, the call silently never runs, and in an agentic loop that text
pollutes later turns. It can also leak `<thinking>` tags into output. If a stage needs to
be cheaper, lower `effort`; do not turn thinking off. (Disabling thinking is in any case
rejected with a 400 at effort `xhigh` or `max`.)

### `output_config={"effort": ...}`

Controls reasoning depth and total token spend. Nested inside `output_config`, not
top-level.

| Stage | Effort | Why |
|---|---|---|
| Bug / security / performance agents | `high` | The precision-critical path |
| Judge | `high` | Final arbiter; the most expensive place to be wrong |
| Evidence agent | `medium` | Mostly interpreting deterministic tool output |
| Planner | `medium` | Routing decisions, not analysis |
| Classification fallback | `low` | Mechanical |

`low` and `medium` are unusually strong on Opus 5 — do not assume `xhigh` is the right
default. Phase 16 exists to settle this empirically per stage; sweep `medium`, `high`,
and `xhigh` against the dataset rather than picking by feel. Reserve `max` for
investigating a specific hard case.

### Structured output, not prompt-and-parse

Response shape is enforced by the API against a JSON Schema derived from Pydantic
models. `client.messages.parse()` returns `parsed_output` already validated — there is no
`json.loads`, no regex extraction, no retry-on-parse-failure loop, and no
"output ONLY valid JSON" instruction in the prompt.

```python
from typing import Literal
from pydantic import BaseModel, Field


class Finding(BaseModel):
    file: str = Field(description="Repository-relative path, exactly as in the diff")
    line: int = Field(description="Line number in the new version of the file")
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
    category: Literal["BUG", "SECURITY", "PERFORMANCE", "RELIABILITY", "MAINTAINABILITY"]
    title: str = Field(description="One line, max 100 characters")
    description: str = Field(description="What is wrong, why it matters, when it fires")
    confidence: float = Field(ge=0.0, le=1.0)


class FindingsReport(BaseModel):
    findings: list[Finding]
```

Schema constraints the API does not support (`minLength`, `maximum`, complex array
constraints) are stripped from the schema sent upstream and validated client-side by the
SDK — so keep using them, but do not rely on the model having seen them. Recursive
schemas are not supported at all.

Two operational notes: a new schema pays a one-time compilation cost on first use and is
then cached for 24 hours, so a schema change shows up as a latency blip. And if
`stop_reason` is `max_tokens` the output may be truncated despite the schema — check it.

**Assistant prefill is not available.** Anything that used to be forced with a partial
assistant turn is done with `output_config.format` or a system-prompt instruction.

### No sampling parameters

`temperature`, `top_p`, and `top_k` are rejected with a 400 on `claude-opus-5`. Determinism
and variance are both steered by prompting and `effort`. There is no
`temperature=0` lever, and there never was a determinism guarantee behind it.

### Streaming above ~16K output

`max_tokens` above roughly 16000 on a non-streaming request risks an SDK HTTP timeout.
Reviewer calls sit at 16000 and stay non-streaming. Any stage that needs more (a large
partial-review batch, a long judge deliberation) uses `client.messages.stream()` with
`.get_final_message()`.

## Prompt caching

Multi-agent review is the ideal caching workload: three or more agents read the *same*
repository context in the same review, and consecutive reviews of the same repository
share the same system prompts. Getting this right is the single largest cost lever
available — cache reads bill at ~0.1× input, writes at ~1.25×.

### The one rule

**Caching is a prefix match. Any byte change anywhere in the prefix invalidates
everything after it.** Render order is `tools` → `system` → `messages`.

### Layout

Order every request from most stable to most volatile:

```
┌─ tools ──────────────────── tool definitions, sorted by name, frozen per agent
├─ system ─────────────────── agent system prompt (frozen text, no interpolation)
│                             ◀── cache breakpoint 1
├─ messages[0] ────────────── repository context bundle for this review
│                             ◀── cache breakpoint 2   (shared across the 3 agents)
└─ messages[0] (last block) ─ the diff and the per-agent question   (uncached)
```

Breakpoint 1 covers tools + system and is reused across every review of every
repository. Breakpoint 2 covers the repository context bundle and is reused by the bug,
security, and performance agents within one review — which is where the multi-agent
architecture pays for itself.

Maximum 4 breakpoints per request. Minimum cacheable prefix on `claude-opus-5` is **512
tokens** (it is 1024 on Sonnet 5 and 4096 on Haiku 4.5 — the minimum is not monotonic
across generations, so a prompt that caches on Opus 5 may silently not cache on Haiku).

### Things that will silently break it

Every one of these has zero error output — just a cache hit rate of zero:

| Do not | Instead |
|---|---|
| Interpolate the PR number, review ID, SHA, or timestamp into the system prompt | Put them in the final uncached message block |
| Build the agent's tool list per-repository | Fixed tool set per agent; sort by name |
| `json.dumps(ctx)` without `sort_keys=True` | Always sort; never serialise a `set` |
| Vary the system prompt by a feature flag | One frozen prompt per agent; branch in the user turn |
| Switch models mid-conversation | Caches are model-scoped; keep a stage on one model |

### Verify it, do not assume it

`llm_calls` records `cache_read_input_tokens` and `cache_creation_input_tokens` per call
(see [03-data-model.md](03-data-model.md)). Phase 15 graphs hit rate per stage. If the
security agent's cache-read tokens are zero while the bug agent's are high, the context
block is being rendered differently between them — diff the rendered bytes.

Note that `input_tokens` is only the *uncached remainder*: total prompt size is
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. A review that
looks suspiciously cheap on `input_tokens` alone is usually just caching well.

### TTL

Default 5 minutes. A single review's three agents run well inside that, so the default is
correct for the intra-review sharing that matters most. `{"type": "ephemeral", "ttl":
"1h"}` doubles the write cost and only pays off across ≥3 reads, so it is worth
considering for the frozen system-prompt breakpoint on a busy installation and not worth
it for per-review context.

## Refusals

Claude Opus 5 ships with elevated cybersecurity safeguards, and a security-focused
reviewer prompt reading attacker-shaped code is exactly the workload that can trip them.
A declined request returns **HTTP 200** with `stop_reason: "refusal"` and a
`stop_details` category — not an exception.

```python
if response.stop_reason == "refusal":
    record_refusal(stage, response.stop_details)
    return FindingsReport(findings=[])  # degrade the stage, not the review
```

**Check `stop_reason` before reading `content` or `parsed_output`.** Code that reads
`content[0]` unconditionally breaks on a refusal, and a refusal is not guaranteed to
carry `stop_details` at all — branch on `stop_reason`, never on `stop_details`.

Opt into server-side fallbacks so a refusal is re-served rather than lost:

```python
response = client.beta.messages.create(
    model="claude-opus-5",
    betas=["server-side-fallback-2026-07-01"],
    fallbacks="default",      # routes by refusal category; no model list to maintain
    ...
)
```

`fallbacks: "default"` picks Anthropic's recommended substitute per category — cyber-
category refusals route to `claude-opus-4-8`. Prefer it over pinning a model: the right
substitute depends on *why* the request was declined, and there is no fallback list to
migrate later. Note the header `server-side-fallback-2026-07-01` gates the `"default"`
scalar form specifically; the older array form uses a different header and pairing them
wrongly returns a 400.

A `stop_reason: "refusal"` on the final response means the whole chain declined. That is
a degraded stage, recorded and surfaced in the review, not a crashed pipeline.

## Prompt authoring rules

Current models follow the system prompt closely and literally. Prompts written to shout
past an older model's reluctance now over-trigger.

- **No pressure language.** `Use this tool when...`, not `CRITICAL: You MUST use...`.
  When several instructions are all marked critical, the marker stops carrying
  information — and an anxious prompt produces a hedging model.
- **No "think step by step".** Redundant on a thinking model; control depth with `effort`.
- **No forced progress narration.** Opus 5 narrates by default; scaffolding like
  "summarise after every 3 tool calls" produces noise.
- **Delete verification instructions.** Opus 5 verifies its own work unprompted; telling
  it to "double-check your answer" causes over-verification with no accuracy gain. This
  inverts the usual self-check advice — the deterministic evidence layer (Phase 7) is
  where verification belongs, not the reviewer's prompt.
- **State the scope explicitly.** Instructions are followed literally and are not
  silently generalised from one item to another.
- **Be explicit about coverage vs filtering.** This one matters a lot for a review
  product: a reviewer told "only report high-severity issues" or "be conservative" will
  investigate just as thoroughly, find the bug, and then decline to report it. Measured
  recall drops while nothing about bug-finding changed. So the reviewer agents are told
  to report everything with a confidence and severity attached, and **all filtering
  happens in the judge** (Phase 8) — which is the architecture anyway.
- **Constrain deliverable length.** Descriptions are posted to GitHub verbatim, and the
  default response length is generous. Cap it in the prompt, and re-check it after any
  model change.

Prompts are versioned files in `agent/prompts/`, one per agent, with the version recorded
on every `llm_calls` row so a precision regression can be traced to a prompt change.

## Cost model

Rough per-review arithmetic for a 7-file, 290-line PR at the phase-14 target:

| Stage | Model | Input | Cached | Output | Cost |
|---|---|---|---|---|---|
| Planner | Opus 5 | 4K | 12K | 1K | ~$0.05 |
| Bug agent | Opus 5 | 2K | 24K | 3K | ~$0.10 |
| Security agent | Opus 5 | 2K | 24K (read) | 3K | ~$0.10 |
| Performance agent | Opus 5 | 2K | 24K (read) | 2K | ~$0.07 |
| Evidence | Opus 5 | 6K | 8K | 2K | ~$0.08 |
| Judge | Opus 5 | 8K | 8K | 2K | ~$0.10 |
| | | | | | **~$0.50** |

Without the shared context breakpoint the three reviewers each pay full input price for
the same 24K of context — roughly $0.36 extra per review, a ~70% increase. That is the
caching argument in one number.

Caps enforced per review, configurable, defaults in
[05-configuration.md](05-configuration.md):

- `MAX_COST_PER_REVIEW_USD` — hard stop; the review completes as `partial`
- `MAX_LLM_CALLS_PER_REVIEW` — runaway-loop backstop
- `MAX_TOKENS_PER_REVIEW` — input + output ceiling
- `MAX_COST_PER_DAY_USD` — installation-level circuit breaker (Phase 13)

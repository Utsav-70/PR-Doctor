# Phase 5 — The First AI Reviewer

## Goal

One agent. One LangGraph pipeline. A PR goes in, a validated `Finding` comes out. Nothing
is posted to GitHub yet.

## Depends on

Phase 4. **Unlocks** Phase 6.

## Why one agent first

Three agents introduced simultaneously means three prompts, three output schemas, a
fan-out, and a merge — with no known-good baseline to compare against. One agent
establishes the schema, the context format, the validation path, the cost per review, and
the latency profile. Phase 6 then fans out a unit that already works.

## Scope

**In:** the LangGraph graph, the Code Review Agent, the Pydantic output schema, structured
output enforcement, findings persisted to Postgres, per-call cost recording.

**Out:** Specialised agents (Phase 6). Evidence verification (Phase 7). Judging (Phase 8).
Posting (Phase 9). The findings this phase produces are unverified hypotheses stored in
the database — which is exactly the right place for them.

## The graph

```
        START
          │
          ▼
     Fetch PR              ← loads PullRequestContext from Postgres
          │
          ▼
   Build Context           ← tool gateway + context builder (Phase 4)
          │
          ▼
   Review Code             ← the single LLM call
          │
          ▼
  Validate Output          ← schema, line existence, in-diff check
          │
          ▼
         END
```

Four nodes. Deliberately linear — LangGraph's value here is not branching, it is the
typed state, the checkpointer, and having the pipeline shape be data rather than nested
function calls. Phase 6 adds the fan-out; Phase 13 adds checkpoint resume.

### Graph state

```python
class ReviewState(TypedDict):
    review_id: str
    pr: PullRequestContext  # from Phase 3
    context_bundle: ContextBundle  # from Phase 4
    findings: list[Finding]
    dropped: list[DroppedFinding]  # with reasons — never silently discard
    errors: list[StageError]
    usage: UsageAccumulator  # tokens and cost per stage
```

State is append-only per node. A node that fails appends to `errors` and returns; it does
not raise. A single failed node degrades the review rather than losing it.

## The agent

**Model:** `claude-opus-5`. **Effort:** `high`. **Thinking:** adaptive.

Full request shape and the reasoning behind every parameter is in
[04-llm-strategy.md](../04-llm-strategy.md). The essentials:

```python
response = client.messages.parse(
    model="claude-opus-5",
    max_tokens=16000,
    system=[{"type": "text", "text": CODE_REVIEW_PROMPT, "cache_control": {"type": "ephemeral"}}],
    thinking={"type": "adaptive"},
    output_config={"effort": "high", "format": FindingsReport},
    messages=[
        {
            "role": "user",
            "content": [
                {"type": "text", "text": context_block, "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": diff_block},
            ],
        }
    ],
)
```

### Output schema

```python
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

`messages.parse()` returns `response.parsed_output` already validated against this
schema. There is no `json.loads`, no regex extraction, no retry-on-parse loop, and the
prompt says nothing about JSON.

An empty `findings` list is a valid and common answer. A reviewer that always finds
something is a reviewer that invents things.

### Prompt design

The system prompt (`agent/prompts/code_review.md`, versioned, recorded on every
`llm_calls` row) states:

- **The role and the standard.** What counts as a finding: something that will cause
  incorrect behaviour, a security exposure, a measurable performance problem, or a
  missing error path. Not style, not naming, not "consider extracting a helper".
- **The scope.** Only lines added or modified in this diff. Pre-existing problems in
  surrounding context are out of scope, however tempting.
- **Report everything, filter nowhere.** This is the important one. A reviewer told "only
  report high-severity issues" or "be conservative" will find the bug and then decline to
  report it — measured recall collapses while nothing about its analysis changed. So:
  report every issue found, attach an honest `confidence` and `severity`, and let the
  judge (Phase 8) filter. Coverage here, filtering there.
- **Cite precisely.** Every finding names a file and a line that exists in the diff.
- **Length.** Descriptions are posted verbatim to GitHub: two to four sentences, what is
  wrong, why it matters, under what conditions it fires.

The prompt does **not** contain: `CRITICAL:` / `YOU MUST` emphasis, "think step by step",
"double-check your answer", or instructions to output JSON. Each of those is either
redundant or actively harmful on this model — see the prompt-authoring rules in
[04-llm-strategy.md](../04-llm-strategy.md).

### Tool use

The agent gets read-only tools from the Phase 4 gateway: `read_file`, `search_code`,
`get_symbol`, `find_references`, `get_tests`. The context bundle covers the common case;
tools handle the rest. Use the SDK's tool runner rather than hand-rolling the loop, and
cap iterations so a confused agent cannot spin.

Tool definitions must be **byte-identical and sorted** across calls — they render at
position 0 of the prompt, so any variation invalidates the entire cache.

## Validation

`Validate Output` is deterministic and mandatory. Structured output guarantees the
*shape*; it guarantees nothing about the *content*.

| Check | On failure |
|---|---|
| `file` exists in `review_files` | Drop, reason `unknown_file` |
| The file was actually reviewed | Drop, reason `file_not_reviewed` |
| `line` exists in the file at head SHA | Drop, reason `line_out_of_range` |
| `line` is in `diff_line_map` (i.e. inside the diff) | Drop, reason `outside_diff` |
| `title` ≤ 100 chars, non-empty | Truncate title; drop if empty |
| `description` non-empty and under the length cap | Truncate; drop if empty |
| `confidence` in `[0, 1]` | Clamp |
| Duplicate `dedupe_key` within this report | Drop the lower-confidence one |

Dropped findings go into `state["dropped"]` with their reason and are persisted with a
`rejection_code`. The drop-rate breakdown is the single most useful diagnostic in the
phase: a high `outside_diff` rate means the context bundle is presenting surrounding
context indistinguishably from changed lines.

Also handle refusals before reading output at all:

```python
if response.stop_reason == "refusal":
    state["errors"].append(StageError("review", "refusal", response.stop_details))
    return state  # degraded stage, not a failed review
```

## Persistence and accounting

- One `findings` row per surviving finding, with `verified` and `judge_accepted` null —
  those belong to later phases.
- One `llm_calls` row per API call: model, stage, input/output tokens, both cache token
  fields, cost, duration, `stop_reason`.
- Enforce `MAX_LLM_CALLS_PER_REVIEW` and `MAX_COST_PER_REVIEW_USD` as the calls happen,
  not after. Exceeding either ends the stage cleanly and marks the review partial.

Verify the cache is working from the first day: on the second review of the same
repository, `cache_read_input_tokens` on the system-prompt breakpoint should be
substantial. If it is zero, something is interpolating into the prefix.

## Exit criteria

- [ ] `review_pull_request` runs the LangGraph pipeline end to end
- [ ] A PR with a deliberately planted bug produces a `findings` row identifying it
- [ ] All output is schema-validated; no manual JSON parsing anywhere in the codebase
- [ ] Every validation rule is exercised at least once in `DRY_RUN`; drops recorded with reasons
- [ ] Findings outside the diff are dropped, always
- [ ] A clean PR produces zero findings without error
- [ ] Refusals degrade the stage and are recorded
- [ ] `llm_calls` rows carry accurate tokens and cost
- [ ] Prompt cache hit rate on the system breakpoint > 0 on the second review
- [ ] Per-review cost for a ~300-line PR recorded as the baseline
- [ ] Nothing is posted to GitHub

## Risks

**Prompting for JSON instead of using structured output.** Produces a parse-retry loop
that mostly works and occasionally corrupts a review. Use `output_config.format`.

**Reading `content[0]` without checking `stop_reason`.** A refusal is HTTP 200 with empty
or partial content; unconditional indexing raises in production and not in tests.

**Telling the reviewer to be conservative.** It will comply by staying silent about real
bugs, and the metric will look like a capability problem. Filtering is Phase 8's job.

**Skipping the in-diff validation.** The most common source of nonsense comments once
Phase 9 lands: a correct observation about a line nobody in this PR touched.

**Non-deterministic context rendering.** Silently triples the cost of Phase 6. Add the
byte-identical-render test now, while there is only one consumer.

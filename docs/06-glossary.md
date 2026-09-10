# Glossary

Terms used with precise, consistent meaning across these docs. Where a word could mean
two things, this file picks one.

**Agent** — An LLM-backed component with a single system prompt, a defined tool set, and
a structured output schema. `bug`, `security`, `performance`, `evidence`, `judge`,
`planner`. Not a Celery task, not a process.

**Base SHA** — The commit the PR branch is compared against. Findings are never reported
against lines that exist unchanged at the base SHA.

**Candidate finding** — A finding as emitted by a reviewer agent, before evidence
verification. Always a hypothesis.

**Confidence** — A number in `[0, 1]`. Three distinct values exist and are never
conflated: `confidence` (the agent's self-report), `verified_confidence` (after
evidence), and `judge_confidence` (final, the one compared against the threshold).

**Context bundle** — The assembled repository context sent to the reviewer agents for a
single review: relevant file excerpts, symbol definitions, references, and related tests.
Built by `agent/context/builder.py`, cached as one prompt-cache prefix shared by all
three reviewers.

**Dead letter** — A task that exhausted its retries. Persisted with enough payload to
replay, never silently discarded.

**Diff position** — GitHub's coordinate system for review comments: an offset into the
unified diff hunk, *not* a file line number. Converting file line → diff position is what
`review_files.diff_line_map` exists for.

**Evidence** — The output of a deterministic verification attempt on a finding. One
finding has many evidence rows, each with an outcome of `supports`, `refutes`,
`inconclusive`, or `error`.

**False positive** — A posted finding that describes a problem that does not exist. The
metric PRGuard is primarily optimised against. Distinct from a *rejected* finding, which
never reached the developer and therefore cost nothing.

**Fan-out** — The parallel execution of the three reviewer agents over the same context
bundle, inside a single Celery task. Not sub-tasks.

**Finding** — A single reviewable issue: file, line, severity, category, title,
description, confidence. The unit of everything downstream of Phase 5.

**Guardrail** — A deterministic check that gates data flow. *Input* guardrails run before
context reaches the model (size, type, injection, secrets). *Output* guardrails run
before a finding reaches GitHub (schema, line validity, dedupe, threshold).

**Head SHA** — The commit actually reviewed. Part of the review idempotency key: a force-
push produces a new head SHA and therefore a new review.

**Hunk** — A contiguous block of changes in a unified diff, with its `@@` header giving
old and new line ranges.

**Idempotency key** — `(repository_id, pr_number, head_sha)`. A redelivered webhook
upserts the existing review rather than creating a second one.

**Judge** — The final decision-making agent. The only component that decides whether a
finding is posted. Rejects duplicates, weak evidence, irrelevance, and false positives.

**LLM gateway** — `agent/llm/`. The single path to the Anthropic API: model routing,
retries, timeouts, token and cost accounting, cache configuration. No agent constructs an
API client itself.

**Partial review** — A review that deliberately stopped short: diff too large, cost cap
hit, or soft timeout reached. Posts whatever was judged and is labelled as partial in the
review summary. Distinct from `failed`, which posts nothing.

**Planner** — The agent that decides which reviewers to run and what context each needs.
Introduced in Phase 6.

**Precision** — `accepted findings that are real / accepted findings`. The primary
metric.

**Recall** — `real bugs found / real bugs present`. Secondary to precision, measured
against the Phase 16 ground-truth dataset.

**Reviewer agent** — One of the three specialists: bug, security, performance. Excludes
the evidence agent, judge, and planner.

**Sandbox** — A per-job Docker container with CPU, memory, PID, time, filesystem, and
network limits, running as a non-root user. The only place PR code is ever executed.

**Tool** — A function exposed to agents through the tool gateway: `read_file`,
`search_code`, `get_symbol`, `find_references`, `get_tests`, and (from Phase 11)
`run_tests`. Agents never touch the filesystem directly.

**Tool gateway** — `agent/tools/gateway.py`. Dispatches tool calls, enforces permissions
and rate limits, records every call to `tool_calls`, and routes sandboxed tools into the
container.

**Unverified finding** — A finding for which no deterministic check could be applied.
Not automatically rejected, but held to a higher confidence bar by the judge.

**Verified** — A finding with at least one `supports` evidence row and no `refutes` row.

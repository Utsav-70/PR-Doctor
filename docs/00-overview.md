# Overview

## What PRGuard is

A GitHub App that watches pull requests and posts review comments. When a PR is opened
or updated, PRGuard fetches the diff, gathers only the repository context that matters,
runs specialised review agents over it, verifies each candidate finding against
deterministic tooling, and posts the survivors as inline review comments.

## Who it is for

Engineering teams that already do human code review and want a first pass that catches
the mechanical and the easily-missed: duplicate side effects, unparameterised SQL,
missing error paths, N+1 queries, secrets committed by accident.

PRGuard is not a replacement for human review. It is a filter that makes human review
cheaper by removing the findings a machine can find reliably.

## The problem that shapes the design

An LLM asked to review a diff will produce plausible findings whether or not they are
real. On a 200-line diff a naive reviewer will confidently report a race condition that
cannot occur, a SQL injection in a parameterised query, and an N+1 in a loop that runs
once. Each false positive costs a developer thirty seconds of reading and a small amount
of trust. Spend that trust and the bot gets muted.

So the central engineering problem is not "can the model find bugs" — it is
**"can we prove the finding is real before we say it out loud."**

## Design principles

These are binding. Every phase spec is written to respect them, and a change that
violates one needs a documented reason.

### 1. The LLM proposes; deterministic tooling verifies

An agent's output is a *hypothesis*, never a conclusion. Before a finding reaches GitHub
it must be corroborated by something that cannot hallucinate: a Semgrep rule, an AST
query, a grep hit at a specific line, a failing test. Where no deterministic check
exists, the finding needs a higher confidence bar and is labelled accordingly.

### 2. Precision over recall

A missed bug costs what it would have cost anyway. A false positive costs trust in every
future comment. When the two trade off, choose precision. Phase 16 measures this
explicitly, and the confidence threshold in Phase 9 is the dial.

### 3. Never send the whole repository to the model

Context is fetched through tools, on demand, in response to what the diff actually
touches. A reviewer that reads three relevant files beats one that skims four hundred —
and costs two orders of magnitude less. This is Phase 4's entire reason for existing.

### 4. Treat everything in the repository as untrusted input

A PR author controls the diff, the commit messages, the README, the code comments, and
the string literals. All of it flows into a prompt. All of it is hostile until proven
otherwise. Repository content is data, never instruction — see Phase 12.

### 5. Never execute PR code in the API or worker process

Running tests against an untrusted branch means running attacker-supplied code. That
happens in a sandbox with CPU, memory, time, filesystem, and network limits, or it does
not happen — see Phase 11.

### 6. Deterministic control flow, model-driven judgment

The pipeline's shape is code: LangGraph nodes, Celery tasks, explicit fan-out. What the
model decides is *what to look at* and *what it means*. Orchestration is never left to
the model, because orchestration must be replayable and debuggable.

### 7. Every review is idempotent and traceable

The same `(repository, pr_number, head_sha)` produces one review record. Redelivered
webhooks do not double-review. Every finding traces back through its evidence, its
agent, its prompt, and its cost.

### 8. Bound the cost of every review before starting it

Diff size, token counts, LLM call counts, and dollars are all capped per review. A 6000-
line refactor degrades to a partial review rather than an unbounded bill — see Phase 3
for the first budget and Phase 14 for the full accounting.

## What the product looks like when finished

```
GitHub PR #182
   │
   ▼
PRGuard · reviewed 7 files (+210 −80) in 2m 14s

🔴 HIGH · BUG · payment/service.py:87
Duplicate payment possible on retry
`process_payment` is called before the idempotency key is persisted, so a
retried request creates a second charge. Verified: no unique constraint on
`payments.idempotency_key`; the retry path at client.py:44 reaches this line.

🟡 MEDIUM · RELIABILITY · api/webhooks.py:23
Missing error handling around `json.loads`
A malformed body raises `JSONDecodeError` and returns a 500 rather than a 400.
```

Two comments, both real, both anchored to a line inside the diff, both carrying the
evidence that justified them.

## Non-goals

- Auto-fixing code or opening fix PRs.
- Style and formatting opinions — that is what `ruff format` is for.
- Reviewing languages the code-intelligence layer cannot parse. Phase 4 starts with
  Python; other languages get diff-only review until a Tree-sitter grammar is wired up.
- A web dashboard. Deliberately deferred; observability lands in Grafana (Phase 15)
  before any bespoke UI.

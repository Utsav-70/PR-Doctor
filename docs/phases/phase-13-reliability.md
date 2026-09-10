# Phase 13 — Reliability

## Goal

Make the pipeline production-grade: retries that help rather than amplify, timeouts at
every layer, real idempotency, a dead-letter queue, checkpoint resume, and circuit
breakers.

## Depends on

Phase 12. **Unlocks** Phase 14.

## Flow

```
              Review
                │
                ▼
             Worker
                │
          ┌─────┴─────┐
          │           │
       Success     Failure
          │           │
          ▼           ▼
       Continue      Retry
                       │
                  ┌────┴────┐
                  ▼         ▼
                Success   Failure
                            │
                            ▼
                           DLQ
```

## Scope

**In:** retry policy, layered timeouts, idempotency, the dead-letter queue, LangGraph
checkpointing, circuit breakers, reapers.

**Out:** Cost caps as circuit breakers (Phase 14 — this phase builds the breaker
mechanism; Phase 14 wires cost into it).

## Retries

The rule: **retry transient failures, never retry bugs.** `autoretry_for=(Exception,)`
turns a `KeyError` into three `KeyError`s and a dead letter, wasting a full review's
compute each time.

| Failure | Retry? | Policy |
|---|---|---|
| GitHub 5xx | Yes | 3 attempts, exponential backoff + jitter |
| GitHub 429 / secondary rate limit | Yes | Honour `Retry-After` exactly |
| GitHub 404 (PR or repo deleted) | No | `skipped` |
| GitHub 403 (permission revoked) | No | `skipped`, alert |
| Anthropic 429 / 529 overloaded | Yes | SDK-level, `max_retries=2`, then task-level |
| Anthropic 400 (bad request) | No | Bug — `failed`, alert |
| Anthropic refusal (`stop_reason`) | No | Degraded stage, recorded (Phase 5) |
| Postgres connection error | Yes | 3 attempts, short backoff |
| Postgres constraint violation | No | Bug — `failed` |
| Docker daemon unavailable | Yes | 2 attempts, then degrade evidence |
| Sandbox timeout | No | Evidence `error`; the review continues |
| Soft time limit exceeded | No | Finish as `partial` |
| Unhandled Python exception | No | `failed`, full traceback, alert |

Jitter is not optional. Ten reviews failing on a GitHub blip and all retrying at exactly
30 seconds is a self-inflicted thundering herd.

**Retry the smallest failing unit.** A failed judge call retries the call, not the whole
review — otherwise a retry re-runs three reviewer agents and the entire evidence layer to
recover from one 529. That is the checkpointing argument below.

## Timeouts

Layered, with each inner timeout strictly smaller than its container. A timeout that
exceeds its parent's is not a timeout.

| Layer | Setting | Value |
|---|---|---|
| HTTP request (GitHub) | `GITHUB_TIMEOUT_SECONDS` | 20 s |
| HTTP request (Anthropic) | `LLM_TIMEOUT_SECONDS` | 600 s |
| Single tool call | gateway per-tool | 30 s |
| Ripgrep subprocess | driver | 10 s |
| Semgrep | `SEMGREP_TIMEOUT_SECONDS` | 120 s |
| Sandbox container | `SANDBOX_TIMEOUT_SECONDS` | 300 s |
| Pipeline stage | graph node | 600 s |
| Celery soft limit | `CELERY_TASK_SOFT_TIME_LIMIT` | 1500 s |
| Celery hard limit | `CELERY_TASK_TIME_LIMIT` | 1800 s |

The soft limit raises `SoftTimeLimitExceeded` inside the task, which is caught to finish
cleanly: post whatever the judge already accepted, mark `partial` with reason `timeout`,
clean up the clone and any containers. The hard limit is a `SIGKILL` — nothing runs after
it, which is why the reapers below exist.

Note the SDK's retry behaviour multiplies wall clock: `timeout × (max_retries + 1)`. An
Anthropic call with a 600 s timeout and 2 retries can consume 30 minutes, exceeding the
task limit. Budget for it: either lower the per-call timeout or lower `max_retries`.

## Idempotency

Three independent layers, because each catches a different failure:

**1. Database.** The unique constraint on `(repository_id, pr_number, head_sha)` means a
redelivered webhook upserts. The `xmax = 0` check from
[Phase 1](phase-01-github-webhook.md) decides whether to enqueue.

**2. Redis lock.** `lock:review:{repo}:{pr}:{sha}`, 60 s TTL, held across the
upsert-and-enqueue, so two concurrent deliveries cannot both see themselves as first.

**3. Celery task ID.** `task_id=f"review:{review_id}"` — Celery rejects a duplicate
in-flight task with the same ID.

Posting needs its own idempotency. `acks_late` means a worker killed between "posted to
GitHub" and "task acknowledged" will redeliver, and a naive retry double-posts every
comment. So:

- Record `posted_at` and `github_comment_id` per finding **in the same transaction
  boundary** as the post where possible.
- On retry, check for an existing review posted by the app for this `commit_id` before
  posting. GitHub's reviews list is the authoritative check.
- Make posting a separate Celery task on the `posting` queue with its own idempotency
  key, so the expensive review work is never re-run to recover a failed post.

## Dead-letter queue

A task that exhausts its retries writes to `dead_letters` — task name, payload, attempt
count, last error with traceback, timestamp — and the review is marked `failed`.

Not a Celery-native concept, so implement it in the task's `on_failure` hook. The point
is that failures are *visible and replayable*: a `POST /internal/dead-letters/{id}/replay`
endpoint (authenticated, internal only) re-enqueues one, and a Grafana panel shows the
depth. A DLQ nobody looks at is a delete with extra steps — alert on non-zero depth.

Common patterns and what they mean: a spike after a deploy is a regression; a slow
accumulation from one repository is usually a repository-specific parse failure; a spike
across all repositories is a provider incident.

## Checkpointing

LangGraph's checkpointer, backed by Postgres, saves graph state after each node. This
makes retries cheap and is the highest-value item in the phase.

Without it, a judge failure at minute nine re-runs three reviewer agents and the entire
evidence layer — 90% of the cost, to recover the last 10%. With it, the retry resumes at
the judge node with the accumulated findings and evidence intact.

```python
graph = builder.compile(checkpointer=PostgresSaver(pool))
config = {"configurable": {"thread_id": f"review:{review_id}"}}
```

`thread_id` keyed on the review ID means a retry of the same review resumes; a new head
SHA is a new review and therefore a new thread.

Rules that make this safe:

- **Checkpoint after every node**, not just expensive ones.
- **Nodes must be resumable.** A node that half-wrote to the database and then failed will
  re-run — so writes are either idempotent (upsert) or happen once at the end of the node.
- **Bound checkpoint size.** Do not store the full context bundle in state; store a
  reference and rebuild. Checkpoints are written on every transition and a fat state
  object makes every transition slow.
- **Prune.** Delete checkpoints for completed reviews after 7 days.
- Invalidate the checkpoint when the graph shape changes — a resumed review must not run
  half of the old pipeline and half of the new one. Version the graph and include the
  version in `thread_id`.

## Circuit breakers

Stop calling a dependency that is failing, rather than adding load to an outage.

| Breaker | Opens on | Open behaviour | Half-open probe |
|---|---|---|---|
| Anthropic API | 5 consecutive failures or >50% error rate over 20 calls | Queue reviews; do not consume them | 1 call after 60 s |
| GitHub API | 5 consecutive 5xx | Pause posting; reviews continue | 1 call after 60 s |
| Docker / sandbox | 3 consecutive failures | Skip sandbox evidence, degrade | 1 run after 300 s |
| Per-installation cost | `MAX_COST_PER_DAY_USD` exceeded | Skip reviews with a recorded reason | Resets daily |

State in Redis so it is shared across worker processes — a per-process breaker in a
four-worker deployment opens four times as slowly as intended.

**An open breaker must not silently drop work.** Reviews stay `queued` and resume when
the breaker closes. The only breaker that intentionally skips work is the cost one, and it
records `skipped` with reason `cost_cap_exceeded` so the omission is visible.

Alert on transitions. A breaker opening is the earliest signal of a provider incident.

## Reapers

Celery beat, because `SIGKILL` and OOM skip every `finally` block:

| Reaper | Interval | Action |
|---|---|---|
| Stale reviews | 5 min | `running` with `started_at` older than the hard limit → `failed`, DLQ |
| Orphaned clones | 15 min | Temp directories with no active review → delete |
| Orphaned containers | 5 min | Containers labelled with a completed review ID → force-remove |
| Checkpoint pruning | daily | Delete checkpoints for reviews completed > 7 days ago |
| Cost rollup | hourly | Aggregate `llm_calls` into daily per-installation totals |
| DLQ alert | 5 min | Alert on non-zero depth |

The orphaned-clone reaper is not optional. A private repository clone left on disk after a
worker OOM is a data-at-rest problem that grows silently.

## Exit criteria

- [ ] Retryable and non-retryable failures are classified explicitly; no blanket retry
- [ ] Every layer has a timeout, and every inner timeout is smaller than its container
- [ ] Anthropic `timeout × (retries + 1)` fits inside the task time limit
- [ ] Three idempotency layers verified by replaying a webhook delivery
- [ ] Retry after a successful post does not double-post
- [ ] Failed tasks land in `dead_letters` and can be replayed
- [ ] Checkpoint resume verified: a judge failure re-runs only the judge
- [ ] Checkpoints are pruned and invalidated on graph version change
- [ ] All four breakers implemented with shared Redis state and alerts
- [ ] An open breaker queues work rather than dropping it
- [ ] All six reapers running under beat, each triggered once against stale state
- [ ] No clone or container survives a `SIGKILL`-ed worker beyond one reaper interval

## Risks

**`autoretry_for=(Exception,)`.** Retries bugs, wastes three full reviews per failure,
and fills the DLQ with the same traceback.

**Retrying the whole review for a single stage failure.** The default without
checkpointing, and the reason checkpointing is the phase's headline item.

**No posting idempotency.** `acks_late` plus a post-then-crash sequence double-posts every
comment. The most user-visible reliability bug available.

**Per-process circuit breakers.** Multiply the failure threshold by the worker count and
open far too late.

**Missing reapers.** Every `finally`-based cleanup is bypassed by `SIGKILL`. Assume
cleanup will be skipped and sweep for it.

**Fat checkpoints.** Serialising the full context bundle on every node transition makes
the pipeline slower than the retries it saves.

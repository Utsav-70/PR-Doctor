# Phase 2 — Queue + Worker

## Goal

The worker consumes the job, authenticates to GitHub, fetches the PR, the diff, and the
changed file list, and writes it all to Postgres. Still no AI.

## Depends on

Phase 1. **Unlocks** Phase 3.

## Scope

**In:** `review_pull_request(review_id)` task, App→installation token auth, GitHub REST
client with retries and rate-limit handling, PR/diff/files fetch, persistence, status
transitions.

**Out:** Diff parsing into structured hunks and language classification (Phase 3).
Repository checkout (Phase 4 — the tools need a working tree; this phase only needs the
API).

## The full loop this phase completes

```
PR opened
    ↓
Webhook  →  202
    ↓
Redis
    ↓
Celery worker
    ↓
GitHub API  (PR + diff + files)
    ↓
PostgreSQL
```

Getting this working end to end, with real latency numbers and real failure modes, is the
whole value of the phase. Everything after it is a substitution into a proven pipeline.

## The task

```python
@celery_app.task(
    name="review.pull_request",
    bind=True,
    acks_late=True,
    max_retries=2,
    autoretry_for=(TransientGitHubError, ConnectionError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    soft_time_limit=1500,
    time_limit=1800,
)
def review_pull_request(self, review_id: str) -> None:
    asyncio.run(_review(review_id, attempt=self.request.retries))
```

`acks_late=True` matters: the message is acknowledged only after the task finishes, so a
worker killed mid-review has its job redelivered rather than lost. It requires the task
to be idempotent — which it is, because the review row is keyed on the head SHA and
re-fetching is harmless.

Distinguish retryable from terminal explicitly. A 404 on the PR (deleted) is terminal —
mark `skipped` and return. A 502 is retryable. Never `autoretry_for=(Exception,)`; that
retries bugs.

## Status transitions

```
queued ──▶ running ──┬──▶ completed
                     ├──▶ failed     (record error jsonb, do not raise past retries)
                     └──▶ skipped    (PR deleted, draft, closed since enqueue)
```

Set `running` and `started_at` as the first action, in its own committed transaction, so
a hung task is visible. Phase 13 adds the reaper that finds rows stuck in `running`.

## GitHub authentication

Two-step, and the reason the App private key is the crown jewel:

1. **App JWT** — RS256, signed with the private key, `iss` = App ID, `iat` backdated 60
   seconds to tolerate clock skew, `exp` at most 10 minutes out. Regenerate per use; do
   not cache.
2. **Installation token** — `POST /app/installations/{id}/access_tokens` with the JWT.
   Returns a token valid ~1 hour.

Cache the installation token in Redis at `gh:token:{installation_id}` with a TTL of
`expires_at - now - 300s`. The five-minute safety margin avoids using a token that
expires mid-review. Never persist it to Postgres, never log it, and redact it from
exception messages — an unhandled `httpx` error will happily include the `Authorization`
header in its repr if you let it.

## What to fetch

Three calls, and no more, in this phase:

| Data | Endpoint | Notes |
|---|---|---|
| PR metadata | `GET /repos/{owner}/{repo}/pulls/{n}` | Title, body, author, draft, mergeable, base/head |
| Unified diff | `GET /repos/{owner}/{repo}/pulls/{n}` with `Accept: application/vnd.github.diff` | One request for the whole diff |
| Changed files | `GET /repos/{owner}/{repo}/pulls/{n}/files?per_page=100` | Paginated; includes per-file `patch`, `additions`, `deletions`, `status` |

Prefer the `files` endpoint's per-file `patch` for anchoring comments later, and keep the
whole-diff blob for context assembly. They can disagree on very large PRs where GitHub
truncates — the `files` endpoint caps at 3000 files and omits `patch` for files over
~20K lines. Record which files came back without a patch; Phase 3 marks them unreviewable
rather than pretending they were reviewed.

**Re-fetch the head SHA and compare.** If `pull_request.head.sha` no longer matches the
review's `head_sha`, a newer commit has landed. Mark this review `skipped` with reason
`superseded` and return — the newer `synchronize` delivery has its own review row. This
avoids spending a full review on a commit nobody will look at.

## GitHub client requirements

One `httpx.AsyncClient` per task, reused across the three calls. Behaviour:

- **Timeout:** `GITHUB_TIMEOUT_SECONDS`, per request.
- **Retries:** 3 attempts, exponential backoff with jitter, on 5xx and connection errors.
- **Primary rate limit:** on 403 with `x-ratelimit-remaining: 0`, sleep until
  `x-ratelimit-reset` if that is under 60 seconds; otherwise retry the task later.
- **Secondary rate limit:** on 403 with a `Retry-After` header, honour it exactly. These
  are abuse-detection limits and ignoring them escalates.
- **Pagination:** follow the `Link` header's `rel="next"`; never construct page URLs.
- **Conditional requests:** send `If-None-Match` with a stored ETag where available and
  treat 304 as a cache hit. GitHub does not count 304s against the rate limit, which
  matters once the same PR is reviewed several times.

Log every request at DEBUG with method, path, status, and `x-ratelimit-remaining`. That
last number is the leading indicator of the failure mode you will hit at scale.

## Persistence

Write in one transaction:

- `reviews`: `title`, `description`, `base_sha`, and (Phase 3 fills the counts)
- `review_files`: one row per changed file — `path`, `previous_path`, `change_type`,
  `additions`, `deletions`, `patch`

Store the raw unified diff on the review row for now (a `raw_diff` text column, dropped
in Phase 3 once per-file patches are authoritative), or write it to object storage keyed
by review ID if diffs are large. Do not hold it only in memory — Phase 3 needs to
re-parse it without re-fetching.

Truncate at the caps in `MAX_DIFF_BYTES` and `MAX_FILE_BYTES` and record that truncation
happened. Silent truncation is the failure mode that makes a review look complete when it
is not.

## Exit criteria

- [ ] Opening a PR results in a `completed` review row without manual intervention
- [ ] `review_files` has one correct row per changed file, patches included
- [ ] PR title and body are persisted
- [ ] Installation token is minted, cached in Redis, and reused within its TTL
- [ ] Token never appears in logs or stored exception payloads
- [ ] A 502 from GitHub retries and succeeds; a 404 marks `skipped` without retrying
- [ ] Rate-limit 403 with `Retry-After` is honoured, verifiable in logs
- [ ] Superseded head SHA short-circuits
- [ ] Worker restart mid-task redelivers and completes the job
- [ ] End-to-end p50 latency recorded as the pre-AI baseline

## Risks

**`autoretry_for=(Exception,)`.** Turns a `KeyError` into three `KeyError`s and a
dead letter. Enumerate retryable exceptions.

**Not using `acks_late`.** A worker OOM then silently drops the review with no record
that it was ever attempted.

**Ignoring secondary rate limits.** GitHub responds to repeated violations by
temporarily blocking the App across all installations, not just the offending one.

**Passing the webhook payload through the queue.** It looks convenient and it makes every
retry replay stale state. The task takes an ID.

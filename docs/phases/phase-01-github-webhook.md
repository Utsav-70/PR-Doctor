# Phase 1 — GitHub App + Webhook

## Goal

Opening a pull request on a repository where the app is installed creates a review record
and puts a job on the queue. The endpoint returns 202 in well under a second.

## Depends on

Phase 0. **Unlocks** Phase 2.

## Scope

**In:** GitHub App registration, webhook endpoint, HMAC signature verification, event
parsing, `installations` / `repositories` / `reviews` tables, enqueue, 202.

**Out:** Anything that fetches from GitHub. Anything that calls the LLM. The worker side
of the queue — Phase 2 consumes what this phase produces.

## GitHub App configuration

Create the App under the org (not a personal account) so ownership survives people.

**Permissions** — request the minimum now; adding later forces every installation to
re-approve.

| Permission | Level | Why |
|---|---|---|
| Pull requests | Read & write | Read the PR, post review comments |
| Contents | Read | Fetch the diff and file contents |
| Metadata | Read | Mandatory |
| Checks | Read & write | Reserved for a future check run; cheap to include now |

**Webhook events:** `pull_request` only. Also subscribe to `installation` and
`installation_repositories` — without them the `installations` table goes stale the first
time someone uninstalls.

**Webhook URL:** `https://<host>/webhooks/github`. In development, use a tunnel
(`gh webhook forward` or ngrok) rather than GitHub's redelivery UI — faster loop.

Set a strong random webhook secret. Store the generated private key immediately; GitHub
will not show it again.

## Endpoint contract

`POST /webhooks/github`

**Request headers used:**

| Header | Use |
|---|---|
| `X-Hub-Signature-256` | `sha256=<hex>` HMAC of the raw body |
| `X-GitHub-Event` | Event name |
| `X-GitHub-Delivery` | Delivery UUID — recorded for idempotency and debugging |
| `X-GitHub-Hook-Installation-Target-ID` | Sanity-check against `GITHUB_APP_ID` |

**Responses:**

| Status | When |
|---|---|
| 202 | Accepted and enqueued |
| 200 | Valid event we deliberately ignore (`closed`, draft PR, bot author, other event types) |
| 401 | Signature missing or invalid |
| 422 | Signature valid but payload unparseable |
| 503 | Redis unavailable — GitHub will retry |

Never return 500 for an expected condition. GitHub disables webhook endpoints that fail
persistently, and a 500 storm is how you lose the integration.

## Signature verification

This is the security boundary of the whole system. Get it exactly right:

1. Read the **raw** request body as bytes, before any JSON parsing. FastAPI's
   `await request.body()`. Do not re-serialise the parsed dict — key order and whitespace
   will differ and the MAC will not match.
2. Compute `hmac.new(secret, raw_body, hashlib.sha256).hexdigest()`.
3. Compare with `hmac.compare_digest` against the header value with the `sha256=` prefix
   stripped. **Never** `==` — timing attacks are cheap here.
4. Missing header → 401. No exceptions, no "development mode" bypass. If a bypass exists
   it will eventually be enabled in production.

Log rejections with the source IP and delivery ID at WARNING. A sustained pattern is
either a misconfigured secret or someone probing.

## Handled events

`pull_request` with action in `opened`, `synchronize`, `reopened`. Everything else gets a
200 and no record.

- `opened` — first review
- `synchronize` — new commits pushed; new head SHA, new review
- `reopened` — re-review, because the base may have moved

Skip (200, no record) when:

- `pull_request.draft` is true and `SKIP_DRAFT_PRS`
- The author is a bot (`user.type == "Bot"`) and `SKIP_BOT_AUTHORS`
- The installation is suspended

## Extracted fields

Everything needed to enqueue, and nothing more. No API calls in this phase.

| Field | Payload path |
|---|---|
| Installation ID | `installation.id` |
| Repository ID | `repository.id` |
| Repository full name | `repository.full_name` |
| Default branch | `repository.default_branch` |
| PR number | `pull_request.number` |
| Head SHA | `pull_request.head.sha` |
| Base SHA | `pull_request.base.sha` |
| Action | `action` |

Note `pull_request.head.sha` and *not* `after` — on `synchronize` they usually agree, but
`head.sha` is the field that describes what will actually be reviewed.

## Persistence and idempotency

Upsert in dependency order: installation → repository → review.

```sql
INSERT INTO reviews (id, repository_id, pr_number, head_sha, base_sha,
                     delivery_id, event_action, status)
VALUES (...)
ON CONFLICT (repository_id, pr_number, head_sha) DO UPDATE
  SET updated_at = now()
RETURNING id, (xmax = 0) AS inserted;
```

`inserted` tells you whether this delivery created the row. **Enqueue only when
`inserted` is true, or when the existing row's status is `failed`.** GitHub redelivers on
timeout, and a `synchronize` burst from a rebase can deliver several events within a
second — without this check the same commit is reviewed three times.

Belt-and-braces: take a short Redis lock keyed on
`lock:review:{repo_id}:{pr}:{head_sha}` with a 60-second TTL around the upsert-and-enqueue
so two concurrent deliveries cannot both observe `inserted`.

## Enqueue

```python
review_pull_request.apply_async(
    args=[str(review_id)],
    queue=settings.REVIEW_QUEUE,
    task_id=f"review:{review_id}",  # Celery-level dedupe
)
```

The task takes **only the review ID**. Not the payload, not the diff. The worker reads
what it needs from Postgres — which keeps the queue message small, makes the task
replayable after a code change, and means a retried task sees current state rather than
a stale snapshot.

## What this phase must not do

**No LLM calls.** Obvious but worth stating: the endpoint's job is to accept and hand off.

**No GitHub API calls.** Not even fetching the PR. Every outbound call is latency inside
GitHub's 10-second budget and a failure mode that turns a delivery into a retry.

**No diff parsing.** Phase 3.

## Exit criteria

- [ ] App registered; installed on a test repository
- [ ] Opening a PR produces a `reviews` row with status `queued`
- [ ] Exactly one job appears in the Redis `reviews` queue
- [ ] Endpoint returns 202 in < 200 ms p99 locally
- [ ] Tampered signature → 401, no row created
- [ ] Replaying the same delivery → still one row, still one job
- [ ] Pushing a commit (`synchronize`) → a second row with the new head SHA
- [ ] Closing the PR → 200, no row
- [ ] Uninstalling the app → `installations` row marked, no orphaned records

## Risks

**Parsing the body before verifying it.** The single most common way to get this wrong.
Verify on raw bytes, then parse.

**Enqueueing on every delivery.** Costs real money once Phase 5 lands, and the bug is
invisible until then. Test the redelivery case now.

**Storing installation tokens.** They expire in an hour. Mint on demand from the App JWT
and cache in Redis with a TTL below the expiry — never in Postgres.

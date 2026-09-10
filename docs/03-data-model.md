# Data Model

PostgreSQL is the system of record. Redis holds only ephemeral state (queues, locks,
counters) and can be flushed without data loss.

Tables are introduced progressively — the phase that first needs a table creates it. The
schema below is the end state; each table notes the phase that introduces it.

## Entity overview

```
installations ──┐
                ├──▶ repositories ──▶ reviews ──┬──▶ findings ──▶ evidence
                                                 ├──▶ review_files
                                                 ├──▶ llm_calls
                                                 └──▶ tool_calls
```

## `installations` — Phase 1

One row per GitHub App installation.

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | GitHub's installation ID |
| `account_login` | text | Org or user the app is installed on |
| `account_type` | text | `Organization` \| `User` |
| `suspended_at` | timestamptz null | Set on `installation.suspend` |
| `created_at` / `updated_at` | timestamptz | |

Installation access tokens are **not** stored. They live ~1 hour and are minted on demand
from the App private key and cached in Redis with a TTL shorter than their expiry.

## `repositories` — Phase 1

| Column | Type | Notes |
|---|---|---|
| `id` | bigint PK | GitHub repository ID |
| `installation_id` | bigint FK | |
| `full_name` | text unique | `owner/repo` |
| `default_branch` | text | |
| `private` | boolean | |
| `config` | jsonb | Per-repo overrides: thresholds, path ignores, disabled agents |
| `created_at` / `updated_at` | timestamptz | |

`config` is populated from a `.prguard.yml` in the repository when present. Unknown keys
are ignored, not rejected.

## `reviews` — Phase 1, extended in 3, 13, 14

The central record. One row per `(repository, pr_number, head_sha)`.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `repository_id` | bigint FK | |
| `pr_number` | integer | |
| `head_sha` | text | The commit actually reviewed |
| `base_sha` | text | |
| `delivery_id` | text | GitHub webhook delivery ID that created this row |
| `event_action` | text | `opened` \| `synchronize` \| `reopened` |
| `status` | text | see lifecycle below |
| `title` | text | PR title (Phase 3) |
| `description` | text | PR body (Phase 3) |
| `changed_files` | integer | Phase 3 |
| `added_lines` | integer | Phase 3 |
| `deleted_lines` | integer | Phase 3 |
| `is_partial` | boolean | Budget forced a partial review (Phase 3) |
| `partial_reason` | text null | e.g. `diff_too_large`, `cost_cap`, `timeout` |
| `attempt` | integer | Retry counter (Phase 13) |
| `total_cost_usd` | numeric(10,6) | Phase 14 |
| `total_input_tokens` | bigint | Phase 14 |
| `total_output_tokens` | bigint | Phase 14 |
| `trace_id` | text | OTel trace ID (Phase 15) |
| `started_at` / `finished_at` | timestamptz null | |
| `error` | jsonb null | Type, message, and stage on failure |
| `created_at` / `updated_at` | timestamptz | |

**Unique constraint:** `(repository_id, pr_number, head_sha)`. This is the idempotency
key — a redelivered webhook upserts rather than inserting.

### Review lifecycle

```
queued ──▶ running ──┬──▶ completed
                     ├──▶ partial      (budget or cap hit; some findings posted)
                     ├──▶ failed       (unrecoverable; nothing posted)
                     └──▶ skipped      (draft PR, ignored paths, bot author)
```

`running` → `queued` is a legal transition on retry, with `attempt` incremented.

## `review_files` — Phase 3

One row per changed file. Keeps the diff out of the `reviews` row and makes per-file
queries cheap.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `review_id` | uuid FK | cascade delete |
| `path` | text | Path at head |
| `previous_path` | text null | Set on rename |
| `change_type` | text | `added` \| `modified` \| `removed` \| `renamed` |
| `language` | text | `python`, `javascript`, `dependency`, `documentation`, `config`, `binary`, `unknown` |
| `category` | text | `source` \| `test` \| `dependency` \| `documentation` \| `config` \| `generated` |
| `added_lines` / `deleted_lines` | integer | |
| `patch` | text null | Unified diff hunk; null for binary or oversized files |
| `diff_line_map` | jsonb | `{new_line: position}` for anchoring comments (Phase 9) |
| `reviewed` | boolean | False when skipped by the budget |

`diff_line_map` is critical: GitHub's review API needs a diff *position*, not a file line
number. Computing it once at analysis time avoids re-parsing the patch at post time.

## `findings` — Phase 5, extended in 6, 7, 8

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `review_id` | uuid FK | cascade delete |
| `file_path` | text | |
| `line` | integer | Line in the new file |
| `end_line` | integer null | For multi-line findings |
| `severity` | text | `CRITICAL` \| `HIGH` \| `MEDIUM` \| `LOW` \| `INFO` |
| `category` | text | `BUG` \| `SECURITY` \| `PERFORMANCE` \| `RELIABILITY` \| `MAINTAINABILITY` |
| `title` | text | One line, imperative or declarative, ≤ 100 chars |
| `description` | text | The explanation posted to GitHub |
| `agent` | text | `bug` \| `security` \| `performance` (Phase 6) |
| `confidence` | numeric(4,3) | Agent's self-reported confidence, 0–1 |
| `verified` | boolean null | Null until evidence runs (Phase 7) |
| `verified_confidence` | numeric(4,3) null | Post-evidence confidence (Phase 7) |
| `judge_accepted` | boolean null | Phase 8 |
| `judge_confidence` | numeric(4,3) null | Phase 8 |
| `judge_reason` | text null | Why accepted or rejected (Phase 8) |
| `rejection_code` | text null | `duplicate` \| `weak_evidence` \| `irrelevant` \| `false_positive` \| `outside_diff` \| `below_threshold` |
| `dedupe_key` | text | Hash of `(file, line, category, normalised title)` |
| `posted_at` | timestamptz null | Phase 9 |
| `github_comment_id` | bigint null | Phase 9 |
| `created_at` | timestamptz | |

**Rejected findings are kept, not deleted.** The rejection set is the training data for
Phase 16 and the fastest way to diagnose a precision regression.

## `evidence` — Phase 7

Many rows per finding. Each row is one verification attempt by one method.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `finding_id` | uuid FK | cascade delete |
| `method` | text | `search` \| `ast` \| `semgrep` \| `ruff` \| `bandit` \| `tests` \| `dataflow` |
| `outcome` | text | `supports` \| `refutes` \| `inconclusive` \| `error` |
| `weight` | numeric(4,3) | Method reliability weight applied by the judge |
| `detail` | jsonb | Tool output: rule IDs, matched lines, exit codes, stderr |
| `duration_ms` | integer | |
| `created_at` | timestamptz | |

## `llm_calls` — Phase 14

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `review_id` | uuid FK | |
| `stage` | text | `planner` \| `bug` \| `security` \| `performance` \| `evidence` \| `judge` |
| `model` | text | e.g. `claude-opus-5` |
| `input_tokens` | integer | |
| `output_tokens` | integer | |
| `cache_creation_input_tokens` | integer | |
| `cache_read_input_tokens` | integer | |
| `cost_usd` | numeric(10,6) | |
| `duration_ms` | integer | |
| `stop_reason` | text | Including `refusal` |
| `error` | text null | |
| `created_at` | timestamptz | |

Cache hit rate is `cache_read / (cache_read + input)` per stage. If it is near zero,
something is invalidating the prompt prefix — see [04-llm-strategy.md](04-llm-strategy.md).

## `tool_calls` — Phase 4, extended in 11

The tool gateway's audit log. Answers "what did the agent actually look at".

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `review_id` | uuid FK | |
| `agent` | text | |
| `tool` | text | `read_file` \| `search_code` \| `get_symbol` \| `find_references` \| `get_tests` \| `run_tests` |
| `args` | jsonb | Redacted of anything path-sensitive |
| `allowed` | boolean | False when the permission check denied it (Phase 11) |
| `result_bytes` | integer | |
| `duration_ms` | integer | |
| `created_at` | timestamptz | |

## `dead_letters` — Phase 13

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `review_id` | uuid FK null | |
| `task_name` | text | |
| `payload` | jsonb | Enough to replay the task |
| `attempts` | integer | |
| `last_error` | jsonb | |
| `created_at` | timestamptz | |
| `replayed_at` | timestamptz null | |

## Indexes

```sql
-- idempotency + the hot lookup path
CREATE UNIQUE INDEX reviews_dedupe ON reviews (repository_id, pr_number, head_sha);
CREATE INDEX reviews_status_created ON reviews (status, created_at DESC);

-- reaper: find reviews stuck in running
CREATE INDEX reviews_running_started ON reviews (started_at) WHERE status = 'running';

-- posting path and the dedupe check against prior reviews of the same PR
CREATE INDEX findings_review ON findings (review_id);
CREATE INDEX findings_dedupe ON findings (review_id, dedupe_key);
CREATE INDEX findings_accepted ON findings (review_id) WHERE judge_accepted;

CREATE INDEX evidence_finding ON evidence (finding_id);
CREATE INDEX review_files_review ON review_files (review_id);

-- cost rollups
CREATE INDEX llm_calls_review ON llm_calls (review_id);
CREATE INDEX llm_calls_created_model ON llm_calls (created_at, model);
```

## Migration policy

Alembic, autogenerate reviewed by hand before commit. Rules:

- One migration per phase where possible; never squash a migration that has been applied
  to a shared environment.
- Additive first: add nullable columns, backfill, then add constraints in a second
  migration. The worker and API deploy independently, so every schema change must be
  compatible with the previous code version for one release.
- No destructive migration without an explicit, separately-reviewed change.

## Retention

| Data | Retention | Why |
|---|---|---|
| `reviews`, `findings`, `evidence` | Indefinite | The evaluation dataset and precision history live here |
| `review_files.patch` | 90 days, then nulled | Largest column; regenerable from GitHub while the SHA exists |
| `tool_calls`, `llm_calls` | 180 days | Cost and behaviour forensics |
| `dead_letters` | 30 days after replay | |
| Cloned repository working trees | Deleted when the task ends, always, including on failure | |

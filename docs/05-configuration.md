# Configuration

All configuration is environment variables, loaded through a single
`pydantic-settings` `Settings` class. Nothing reads `os.environ` directly outside that
class — this keeps defaults, validation, and documentation in one place.

Per-repository overrides live in `.prguard.yml` in the reviewed repository and are
narrower in scope: thresholds, path ignores, and agent enablement only. Secrets and
infrastructure are never configurable per repository.

`.env` is git-ignored. `.env.example` is committed with every key present and every
secret value blank.

## Core

| Variable | Default | Notes |
|---|---|---|
| `PRGUARD_ENV` | `development` | `development` \| `staging` \| `production` |
| `LOG_LEVEL` | `INFO` | |
| `LOG_FORMAT` | `json` | `json` \| `console` |

## GitHub App

| Variable | Default | Notes |
|---|---|---|
| `GITHUB_APP_ID` | — | Required |
| `GITHUB_APP_PRIVATE_KEY` | — | Required. PEM contents, newlines escaped as `\n` |
| `GITHUB_WEBHOOK_SECRET` | — | Required. Used for HMAC-SHA256 verification |
| `GITHUB_API_URL` | `https://api.github.com` | Override for GitHub Enterprise |
| `GITHUB_TIMEOUT_SECONDS` | `20` | Per-request |
| `GITHUB_MAX_RETRIES` | `3` | Respects `Retry-After` on secondary rate limits |

Store the private key in a secrets manager in production. It mints installation tokens
for every repository the app is installed on — it is the highest-value secret in the
system.

## Datastores

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://prguard:prguard@postgres:5432/prguard` | |
| `DATABASE_POOL_SIZE` | `10` | Per process |
| `DATABASE_MAX_OVERFLOW` | `5` | |
| `REDIS_URL` | `redis://redis:6379/0` | Celery broker |
| `REDIS_RESULT_URL` | `redis://redis:6379/1` | Celery results |
| `REDIS_LOCK_URL` | `redis://redis:6379/2` | Idempotency locks, counters |

## Celery

| Variable | Default | Notes |
|---|---|---|
| `CELERY_WORKER_CONCURRENCY` | `4` | Reviews are IO-heavy but hold a clone each |
| `CELERY_TASK_TIME_LIMIT` | `1800` | Hard kill, seconds |
| `CELERY_TASK_SOFT_TIME_LIMIT` | `1500` | Raises, allowing a clean partial review |
| `CELERY_TASK_MAX_RETRIES` | `2` | Then dead-letter (Phase 13) |
| `CELERY_PREFETCH_MULTIPLIER` | `1` | Long tasks — do not hoard |
| `REVIEW_QUEUE` | `reviews` | |
| `POSTING_QUEUE` | `posting` | Separate so posting never queues behind reviews |

## LLM

See [04-llm-strategy.md](04-llm-strategy.md) for the reasoning behind these.

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required. Resolved by the SDK if unset via an `ant auth login` profile |
| `LLM_STRONG_MODEL` | `claude-opus-5` | Reviewers, evidence, judge |
| `LLM_CHEAP_MODEL` | `claude-haiku-4-5` | Mechanical classification only |
| `LLM_REVIEWER_EFFORT` | `high` | |
| `LLM_JUDGE_EFFORT` | `high` | |
| `LLM_EVIDENCE_EFFORT` | `medium` | |
| `LLM_PLANNER_EFFORT` | `medium` | |
| `LLM_MAX_TOKENS` | `16000` | Above ~16000 requires streaming |
| `LLM_TIMEOUT_SECONDS` | `600` | SDK default is 10 minutes |
| `LLM_MAX_RETRIES` | `2` | SDK-level; retries 408/409/429/5xx |
| `LLM_ENABLE_PROMPT_CACHE` | `true` | |
| `LLM_CACHE_TTL` | `5m` | `5m` \| `1h` |
| `LLM_THINKING_DISPLAY` | `omitted` | `summarized` when exporting traces for debugging |
| `LLM_ENABLE_FALLBACKS` | `true` | Server-side refusal fallbacks |

## Budgets and caps

| Variable | Default | Notes |
|---|---|---|
| `MAX_CHANGED_LINES` | `5000` | Above this, partial review (Phase 3) |
| `MAX_CHANGED_FILES` | `100` | |
| `MAX_FILE_BYTES` | `500000` | Per-file read cap in the tool gateway |
| `MAX_DIFF_BYTES` | `2000000` | Total |
| `MAX_CONTEXT_TOKENS` | `60000` | Context bundle ceiling before trimming |
| `MAX_LLM_CALLS_PER_REVIEW` | `25` | Runaway backstop |
| `MAX_TOKENS_PER_REVIEW` | `500000` | Input + output |
| `MAX_COST_PER_REVIEW_USD` | `1.50` | Hard stop → `partial` |
| `MAX_COST_PER_DAY_USD` | `200.00` | Installation circuit breaker (Phase 13) |

## Review behaviour

| Variable | Default | Notes |
|---|---|---|
| `CONFIDENCE_THRESHOLD` | `0.85` | Below this, a finding is not posted (Phase 9) |
| `MAX_COMMENTS_PER_REVIEW` | `15` | Twenty comments is not a review, it is noise |
| `MIN_SEVERITY_TO_POST` | `MEDIUM` | `LOW`/`INFO` are recorded, not posted |
| `SKIP_DRAFT_PRS` | `true` | |
| `SKIP_BOT_AUTHORS` | `true` | Dependabot et al. |
| `IGNORE_PATHS` | `**/migrations/**,**/vendor/**,**/*.lock,**/*.min.js` | Comma-separated globs |
| `ENABLED_AGENTS` | `bug,security,performance` | |
| `REQUIRE_EVIDENCE` | `true` | Unverified findings need a higher confidence bar |
| `DRY_RUN` | `false` | Run the full pipeline, log the comments, post nothing |

`DRY_RUN=true` is how you evaluate a prompt or threshold change against live traffic
without spending anyone's trust. Use it for the first week on any new repository.

## Static analysis (Phase 10)

| Variable | Default | Notes |
|---|---|---|
| `SEMGREP_ENABLED` | `true` | |
| `SEMGREP_CONFIG` | `p/security-audit,p/python` | |
| `SEMGREP_TIMEOUT_SECONDS` | `120` | |
| `RUFF_ENABLED` | `true` | |
| `BANDIT_ENABLED` | `true` | |
| `PYTEST_ENABLED` | `false` | Off by default — see Phase 11 before enabling |
| `PYTEST_TIMEOUT_SECONDS` | `300` | |
| `GITLEAKS_ENABLED` | `true` | Runs before context reaches the LLM (Phase 12) |

## Sandbox (Phase 11)

| Variable | Default | Notes |
|---|---|---|
| `SANDBOX_IMAGE` | `prguard/sandbox:latest` | |
| `SANDBOX_CPU_LIMIT` | `2.0` | Cores |
| `SANDBOX_MEMORY_LIMIT` | `2g` | |
| `SANDBOX_PIDS_LIMIT` | `256` | |
| `SANDBOX_TIMEOUT_SECONDS` | `300` | Wall clock, killed hard |
| `SANDBOX_NETWORK_MODE` | `none` | `none` \| `restricted`. Never `bridge` |
| `SANDBOX_USER` | `10001:10001` | Non-root, always |
| `SANDBOX_READONLY_ROOTFS` | `true` | Writable tmpfs at `/tmp` and the workspace only |

## Observability (Phase 15)

| Variable | Default | Notes |
|---|---|---|
| `OTEL_ENABLED` | `false` | |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | — | |
| `OTEL_SERVICE_NAME` | `prguard` | Suffixed per service |
| `PROMETHEUS_ENABLED` | `true` | |
| `PROMETHEUS_PORT` | `9090` | |
| `LANGFUSE_ENABLED` | `false` | |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_HOST` | — | |

## Validation at startup

`Settings` fails fast. The API and worker refuse to start when:

- Any required variable is missing.
- `GITHUB_APP_PRIVATE_KEY` does not parse as a PEM private key.
- `CONFIDENCE_THRESHOLD` is outside `[0, 1]`.
- `SANDBOX_NETWORK_MODE` is `bridge` or `host` while `PRGUARD_ENV=production`.
- `SANDBOX_USER` resolves to uid 0.
- `LLM_MAX_TOKENS` exceeds 16000 without streaming enabled for that stage.

A misconfigured guardrail must be a boot failure, not a silent downgrade.

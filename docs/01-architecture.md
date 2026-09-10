# Architecture

## Services

Four long-running processes plus two datastores. Everything runs under
`docker-compose.yml` in development.

| Service | Process | Responsibility |
|---|---|---|
| `api` | FastAPI + uvicorn | Receive webhooks, verify signatures, persist review records, enqueue jobs. Never calls the LLM. Never does slow work. |
| `worker` | Celery worker | Runs the review pipeline: fetch, analyse, agents, evidence, judge, post. |
| `beat` | Celery beat | Periodic jobs: stale-review reaper, cost rollups, cache warmers. |
| `postgres` | PostgreSQL 16 | System of record: reviews, findings, evidence, costs. |
| `redis` | Redis 7 | Celery broker + result backend, idempotency locks, rate-limit counters. |
| `sandbox` | Docker (spawned per job) | Untrusted execution: pytest, Semgrep, Bandit. Phase 11. |

The `api` service must stay fast and boring. Its p99 is a GitHub-visible number — GitHub
times webhook deliveries out at 10 seconds and disables endpoints that fail repeatedly.

## End-to-end flow

```
Developer
    │  opens / updates PR
    ▼
GitHub ──webhook──▶ FastAPI  POST /webhooks/github
                       │  verify HMAC signature
                       │  extract repo, pr_number, head_sha
                       │  upsert Review (idempotent on head_sha)
                       │  enqueue review_pull_request(review_id)
                       └──▶ 202 Accepted
                                │
                            Redis queue
                                │
                                ▼
                        Celery worker
                                │
                    ┌───────────┴────────────┐
                    ▼                        ▼
             GitHub REST/GraphQL      Repository checkout
             (PR, diff, files)        (shallow clone at head_sha)
                    │                        │
                    └───────────┬────────────┘
                                ▼
                          PR Analyzer          ← deterministic, no LLM
                    (files, hunks, language
                     classification, budget)
                                │
                                ▼
                       LangGraph pipeline
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
              Context Builder ◀──── Tool Gateway
              (per-file bundles)    read_file · search_code
                    │               get_symbol · find_references
                    │               get_tests
                    ▼
                 Planner
                    │
        ┌───────────┼────────────┐
        ▼           ▼            ▼
     Bug Agent  Security     Performance      ← fan-out, parallel
        │        Agent         Agent
        └───────────┼────────────┘
                    ▼
             Candidate findings
                    │
                    ▼
             Evidence Agent  ──▶ Semgrep · AST · tests · grep
                    │                    (in sandbox)
                    ▼
                  Judge                            ← dedupe, reject, score
                    │
                    ▼
            Output Guardrails                      ← line/diff/threshold checks
                    │
                    ▼
             GitHub Review API
                    │
                    ▼
              PR review comments
```

## Repository layout

```
prguard/
├── apps/
│   ├── api/                    FastAPI application
│   │   ├── main.py             app factory, lifespan, health
│   │   ├── routes/             webhooks, health, internal
│   │   ├── deps.py             DI: db session, redis, settings
│   │   └── schemas/            request/response models
│   └── worker/                 Celery application
│       ├── celery_app.py       broker/backend config, queues, routing
│       ├── tasks/              review_pull_request, post_review, reapers
│       └── pipeline.py         entry point into the LangGraph graph
│
├── agent/
│   ├── agents/                 one module per reviewer
│   │   ├── planner.py
│   │   ├── bug.py
│   │   ├── security.py
│   │   ├── performance.py
│   │   ├── evidence.py
│   │   └── judge.py
│   ├── graph/                  LangGraph wiring
│   │   ├── state.py            the typed graph state
│   │   ├── nodes.py            node functions
│   │   └── build.py            graph construction + checkpointer
│   ├── tools/                  the tool gateway
│   │   ├── gateway.py          dispatch, permissions, audit log
│   │   ├── read_file.py
│   │   ├── search_code.py      ripgrep
│   │   ├── symbols.py          Tree-sitter: get_symbol, find_references
│   │   └── tests.py            get_tests
│   ├── context/                context assembly + token budgeting
│   │   ├── builder.py
│   │   ├── budget.py
│   │   └── render.py           prompt-cache-friendly serialisation
│   ├── llm/                    LLM gateway (Phase 14)
│   │   ├── client.py           Anthropic client, retries, timeouts
│   │   ├── router.py           cheap vs strong model selection
│   │   └── accounting.py       token/cost recording
│   └── prompts/                system prompts, one file per agent
│
├── github/                     GitHub integration
│   ├── app_auth.py             App JWT → installation token
│   ├── client.py               REST/GraphQL wrapper, retries, rate limits
│   ├── webhooks.py             signature verification, event parsing
│   ├── diff.py                 unified-diff parsing, line mapping
│   └── review.py               posting reviews and comments
│
├── db/
│   ├── models.py               SQLAlchemy models
│   ├── session.py              engine + sessionmaker
│   ├── repositories/           query layer
│   └── migrations/             Alembic
│
├── security/
│   ├── input_guard.py          size/type/binary/generated-file gates
│   ├── injection.py            prompt-injection defences
│   ├── output_guard.py         schema, line, dedupe, threshold checks
│   ├── secrets.py              Gitleaks integration
│   └── sandbox.py              Docker sandbox driver
│
├── evaluation/
│   ├── dataset/                pr_001/ … pr_100/ ground truth
│   ├── runner.py               replay a dataset through the pipeline
│   ├── metrics.py              precision, recall, F1, FPR, latency, cost
│   └── report.py
│
├── observability/
│   ├── tracing.py              OpenTelemetry setup
│   ├── metrics.py              Prometheus registry
│   ├── langfuse.py             LLM trace export
│   └── logging.py              structlog, correlation IDs
│
├── samples/                    sample repos + PRs used while building
│   ├── repo/                   known symbols, call sites, tests (Phase 4)
│   ├── injection/              prompt-injection vectors (Phase 12)
│   └── adversarial/            sandbox escape attempts (Phase 11)
│
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## Boundaries that matter

**`apps/` depends on everything; nothing depends on `apps/`.** The API and worker are
entry points. Business logic lives in `agent/`, `github/`, `db/`, `security/`.

**`agent/` never imports from `github/` directly.** The pipeline receives an already-
fetched, already-analysed `PullRequestContext`. This keeps the graph testable against
fixtures and makes a future GitLab adapter a matter of writing one module.

**`agent/tools/` is the only path to the filesystem or the repository.** Agents never
open files. Every access goes through the gateway so it can be permission-checked,
rate-limited, audited, and — from Phase 11 — sandboxed.

**`security/output_guard.py` is the only writer path to GitHub.** Nothing posts a comment
without passing through it.

## Concurrency model

- One Celery task per review. A review is a single unit of work with a single timeout.
- Fan-out to the three reviewer agents happens *inside* the task, via `asyncio.gather`
  over the LangGraph parallel edges — not by spawning sub-tasks. This keeps the review
  atomic and the trace contiguous.
- Sandboxed tool calls (pytest, Semgrep) are blocking subprocesses with hard timeouts,
  run in a thread pool so they do not stall the event loop.
- Queue separation: `reviews` (long, expensive) and `posting` (short, latency-sensitive)
  are separate Celery queues with separate concurrency limits, so a backlog of reviews
  never delays posting a finished one.

## Failure posture

| Failure | Behaviour |
|---|---|
| Signature verification fails | 401, no record created, logged with source IP |
| Redis unavailable at enqueue | 503 — GitHub retries the delivery |
| GitHub API 5xx / secondary rate limit | Retry with backoff, respect `Retry-After` |
| One reviewer agent fails | Continue with the others; record the gap in the review |
| Evidence tooling fails | Finding drops to unverified; judge applies the higher bar |
| Judge fails | Post nothing; mark review `failed`. Never post unjudged findings. |
| Task exceeds wall clock | Kill, retry once, then dead-letter (Phase 13) |
| Cost cap exceeded mid-review | Stop cleanly, post what was already judged, flag as partial |

The invariant: **a broken PRGuard posts nothing, not something wrong.**

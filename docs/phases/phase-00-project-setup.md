# Phase 0 — Project Setup

## Goal

A running skeleton. Four containers come up, the API answers a health check, the worker
connects to the broker, and migrations apply against Postgres. No features.

## Depends on

Nothing. **Unlocks** everything.

## Scope

**In:** directory tree, dependency pinning, `docker-compose.yml`, settings class, DB
session + Alembic bootstrap, Celery app, health endpoint, linting, test harness, CI.

**Out:** GitHub integration, any LLM code, any business logic. Resist the urge to write
`agent/` code now — the empty packages are placeholders on purpose.

## Stack

| Concern | Choice | Note |
|---|---|---|
| Language | Python 3.12 | |
| API | FastAPI + uvicorn | |
| DB | PostgreSQL 16 | |
| ORM | SQLAlchemy 2.0 (async) + Alembic | |
| Queue | Celery 5 with Redis broker and result backend | |
| Cache / locks | Redis 7 | |
| Orchestration | LangGraph | Wired in Phase 5 |
| LLM | `anthropic` SDK | Used from Phase 5 |
| Validation | Pydantic v2 + `pydantic-settings` | |
| Lint / format | Ruff | |
| Types | mypy, strict on `agent/`, `db/`, `security/` |
| Container | Docker + Compose | |

## Directory tree

Create the tree from [01-architecture.md](../01-architecture.md) with an `__init__.py` in
every package. Empty packages get a one-line docstring stating which phase fills them —
that turns the skeleton into a map.

```
prguard/
├── apps/{api,worker}/
├── agent/{agents,graph,tools,context,llm,prompts}/
├── github/
├── db/{repositories,migrations}/
├── security/
├── evaluation/dataset/
├── observability/
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## Design notes

**Async all the way down.** FastAPI is async; SQLAlchemy uses the async engine with
`psycopg`. Celery is not async — the worker runs sync tasks that drive the async pipeline
via a single `asyncio.run()` per task. Do not mix: one event loop per task, created at the
task boundary.

**Two Dockerfiles or one with targets.** API and worker share dependencies but not
entrypoints. A single multi-stage Dockerfile with `api` and `worker` targets keeps the
dependency layer cached across both.

**Settings is a singleton, created once.** `get_settings()` with `@lru_cache`, injected
into FastAPI via `Depends`, imported directly in the worker. It validates at import
time — a bad config is a boot failure (see [05-configuration.md](../05-configuration.md)).

**Alembic against the async engine** needs `run_async_migrations` in `env.py`. Set this
up now; retrofitting it after ten migrations is unpleasant.

## `docker-compose.yml` shape

```yaml
services:
  postgres:    # 16-alpine, healthcheck: pg_isready
  redis:       # 7-alpine, healthcheck: redis-cli ping
  api:         # depends_on both healthy; uvicorn --reload; port 8000
  worker:      # depends_on both healthy; celery -A apps.worker.celery_app worker
  beat:        # celery beat, added properly in Phase 13
volumes:
  pgdata:
```

Mount the source read-write in development so `--reload` works. Do not mount source in
the production image.

## Health endpoint

`GET /health` returns 200 with:

```json
{"status": "ok", "version": "0.1.0", "checks": {"database": "ok", "redis": "ok"}}
```

Return 503 if either check fails. Keep it cheap — `SELECT 1` and `PING`, both with a
1-second timeout. This endpoint is what the compose healthcheck and, later, the load
balancer poll; it must never touch GitHub or the LLM.

Add `GET /health/live` (process is up, no dependency checks) as well — liveness and
readiness need different semantics the moment there is more than one replica.

## Exit criteria

- [ ] `docker compose up` reaches all services healthy from a clean volume
- [ ] `curl localhost:8000/health` → 200 with both checks `ok`
- [ ] `alembic upgrade head` applies against the compose Postgres
- [ ] `alembic downgrade base` then `upgrade head` round-trips cleanly
- [ ] Worker logs show it connected to Redis and registered its task modules
- [ ] `ruff check`, `ruff format --check`, and `mypy` all pass
- [ ] CI runs `ruff check`, `ruff format --check`, and `mypy` on push
- [ ] `.env.example` lists every key; `.env` is git-ignored
- [ ] `README.md` has a working five-line quickstart

## Risks

**Skipping type checking now.** Adding mypy to a 5000-line codebase at Phase 10 is a
week of work. Adding it to an empty one is twenty minutes — and with no test suite,
`mypy --strict` is the main automated correctness signal this project has.

**Under-specified dependency pins.** Pin exact versions in `requirements.txt` and keep a
separate `requirements-dev.txt`. LangGraph and the Anthropic SDK both move quickly, and an
unpinned upgrade mid-project produces confusing failures in Phase 5.

# Roadmap

17 phases, built in order. Each one ends in something demonstrable — that is the point of
the sequencing. Do not start a phase whose dependencies are not passing their exit
criteria.

## Phase table

| # | Phase | Depends on | Ends when you can demonstrate |
|---|---|---|---|
| 0 | Project setup | — | `docker compose up` brings up api, worker, postgres, redis; health check green |
| 1 | GitHub App + webhook | 0 | Opening a PR creates a `reviews` row and a Redis job; endpoint returns 202 |
| 2 | Queue + worker | 1 | The worker picks up the job, fetches PR + diff + files, writes them to Postgres |
| 3 | PR analyzer | 2 | `reviews` row carries file classifications and line counts; >5000-line diffs mark `partial` |
| 4 | Code intelligence | 3 | Five tools callable in isolation: read, search, symbol, refs, tests |
| 5 | First AI reviewer | 4 | One PR → one validated `Finding` object from a single Code Review Agent |
| 6 | Multi-agent | 5 | Planner fans out to bug/security/performance; findings tagged with their agent |
| 7 | Evidence verification | 6 | Each finding carries `verified` + adjusted confidence, backed by tool output |
| 8 | Judge agent | 7 | Duplicate/weak/irrelevant findings are rejected with recorded reasons |
| 9 | GitHub comments | 8 | Accepted findings appear as inline comments on the PR — **first end-to-end product** |
| 10 | Static analysis | 9 | Semgrep/Ruff/Bandit findings feed evidence; agreement raises confidence |
| 11 | Docker sandbox | 10 | pytest and Semgrep run against PR code in a resource-capped container |
| 12 | Security & guardrails | 11 | Input caps enforced; injection attempts neutralised; Gitleaks blocks secret leakage |
| 13 | Reliability | 12 | Retries, timeouts, idempotency, DLQ, checkpoint resume, circuit breakers |
| 14 | Cost optimization | 13 | Per-review cost recorded and capped; cheap/strong routing measurably cheaper |
| 15 | Observability | 14 | A per-stage latency breakdown for any review ID |
| 16 | Evaluation | 15 | Precision/recall/F1/FPR over 100 historical PRs, reproducible |

## Dependency graph

```
0 ──▶ 1 ──▶ 2 ──▶ 3 ──▶ 4 ──▶ 5 ──▶ 6 ──▶ 7 ──▶ 8 ──▶ 9  ◀── shippable product
                                                          │
                                          ┌───────────────┼───────────────┐
                                          ▼               ▼               ▼
                                        10 ──▶ 11 ──▶ 12   13         14 ──▶ 15
                                          │                               │
                                          └──────────────┬────────────────┘
                                                         ▼
                                                        16  ◀── the feedback loop
```

Phases 0–9 are a strict chain. After 9 there is some freedom, but the recommended order
is as numbered: 10 and 11 sharpen the evidence layer, 12 and 13 harden it, 14 and 15
instrument it, and 16 measures whether any of it worked.

## Three checkpoints worth pausing at

**After Phase 3.** You have a working GitHub App with a real async pipeline and zero AI.
Everything measurable about latency and reliability is measurable now, before the model
makes the traces noisy. Confirm the boring parts are boring.

**After Phase 9.** This is the product. It reviews PRs and posts comments. Run it on your
own repositories for a week and read every comment it makes — that experience is what
tells you where Phases 10–16 need to go.

**After Phase 16.** You have numbers. Every subsequent change to prompts, models,
thresholds, or agents can be justified or rejected against precision, recall, and cost
per review. Before Phase 16, prompt tuning is guesswork.

## Sequencing rules

**Deterministic before probabilistic.** Phases 1–4 have no LLM in them at all. Debugging
a queue while also debugging a prompt is two problems pretending to be one.

**Single agent before many.** Phase 5's single reviewer establishes the schema, the
validation, the context format, and the cost baseline. Phase 6 is then a fan-out of a
known-good unit rather than three simultaneous unknowns.

**Verification before publication.** Phases 7 and 8 come before Phase 9 deliberately.
The first comment PRGuard ever posts should already have been through evidence checking
and a judge — because the first week of comments sets whether anyone trusts the second.

**Sandbox before execution.** Phase 11 precedes any code execution. There is no
intermediate state where PRGuard runs untrusted tests unsandboxed "just for now".

**Measurement last, but measurement.** Phase 16 is last because it needs a complete
pipeline to measure, not because it is optional. Without it, precision is a feeling.

## Deliberately deferred

- **Next.js dashboard.** Not in these 17 phases. Grafana covers operational needs.
- **Non-Python languages.** The Tree-sitter layer is built to be extensible (Phase 4),
  but only Python grammar ships. JS/TS is the natural second.
- **Incremental review of force-pushes.** Each `head_sha` gets a full review. Diffing
  reviews across SHAs to avoid repeating comments is a Phase 9 follow-up, tracked as a
  known limitation.
- **Self-hosted / on-prem model serving.** The LLM gateway (Phase 14) abstracts the
  provider, which is the only preparation needed.

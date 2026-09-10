# PRGuard — Documentation

PRGuard is a GitHub App that reviews pull requests with a multi-agent LLM pipeline,
verifies every finding against deterministic evidence, and posts only what survives
that verification.

> **Naming.** The repository is `PR-Doctor`; the product and the Python package root are
> `prguard`. Docs use *PRGuard* throughout.

## Read in this order

| Doc | What it answers |
|---|---|
| [00-overview.md](00-overview.md) | What PRGuard is, who it is for, the design principles that constrain every phase |
| [01-architecture.md](01-architecture.md) | The full system: services, data flow, request lifecycle, repository layout |
| [02-roadmap.md](02-roadmap.md) | All 17 phases, their dependencies, and the exit criteria for each |
| [03-data-model.md](03-data-model.md) | PostgreSQL schema, entity lifecycles, migration policy |
| [04-llm-strategy.md](04-llm-strategy.md) | Model choice, structured output, prompt caching, cost control |
| [05-configuration.md](05-configuration.md) | Every environment variable and its default |
| [06-glossary.md](06-glossary.md) | Terms used consistently across these docs |

## Phase specifications

Each phase has its own spec with scope, design, and exit criteria. Build them in order —
every phase assumes the previous ones work.

| # | Phase | Doc |
|---|---|---|
| 0 | Project setup | [phases/phase-00-project-setup.md](phases/phase-00-project-setup.md) |
| 1 | GitHub App + webhook | [phases/phase-01-github-webhook.md](phases/phase-01-github-webhook.md) |
| 2 | Queue + worker | [phases/phase-02-queue-worker.md](phases/phase-02-queue-worker.md) |
| 3 | PR analyzer | [phases/phase-03-pr-analyzer.md](phases/phase-03-pr-analyzer.md) |
| 4 | Code intelligence layer | [phases/phase-04-code-intelligence.md](phases/phase-04-code-intelligence.md) |
| 5 | First AI reviewer | [phases/phase-05-first-reviewer.md](phases/phase-05-first-reviewer.md) |
| 6 | Multi-agent architecture | [phases/phase-06-multi-agent.md](phases/phase-06-multi-agent.md) |
| 7 | Evidence verification | [phases/phase-07-evidence.md](phases/phase-07-evidence.md) |
| 8 | Judge agent | [phases/phase-08-judge.md](phases/phase-08-judge.md) |
| 9 | GitHub comments | [phases/phase-09-github-comments.md](phases/phase-09-github-comments.md) |
| 10 | Static analysis | [phases/phase-10-static-analysis.md](phases/phase-10-static-analysis.md) |
| 11 | Docker sandbox | [phases/phase-11-sandbox.md](phases/phase-11-sandbox.md) |
| 12 | Security & guardrails | [phases/phase-12-guardrails.md](phases/phase-12-guardrails.md) |
| 13 | Reliability | [phases/phase-13-reliability.md](phases/phase-13-reliability.md) |
| 14 | Cost optimization | [phases/phase-14-cost.md](phases/phase-14-cost.md) |
| 15 | Observability | [phases/phase-15-observability.md](phases/phase-15-observability.md) |
| 16 | Evaluation | [phases/phase-16-evaluation.md](phases/phase-16-evaluation.md) |

## Verifying a change

This project does not carry a test suite, by choice. What replaces it:

| Tool | Catches |
|---|---|
| `ruff check` + `ruff format --check` | Style, obvious errors, unused code |
| `mypy --strict` on `agent/`, `db/`, `github/` | Type and contract errors — the main automated signal here |
| `DRY_RUN=true` on a real PR | Review quality. Runs the whole pipeline, logs the comments it would post, posts nothing |
| Sample sets under `samples/` | Sandbox escapes (Phase 11), prompt injection (Phase 12) |
| Phase 16 evaluation dataset | Precision, recall, false positives per clean PR |

Three things genuinely need a manual look, because nothing above catches them and each
fails silently:

1. **Diff position mapping** (Phase 3) — an off-by-one anchors comments to the wrong line
   and still looks plausible. Check the computed map against a real diff with multiple
   hunks in one file, and one with a hunk starting at line 1.
2. **Sandbox limits** (Phase 11) — run the adversarial sample and confirm the limits with
   `docker inspect`.
3. **Injection framing** (Phase 12) — run the injection samples after any prompt change.

Note that the Phase 16 evaluation dataset is not a test suite. Tests answer "does this
function behave"; the dataset answers "are these reviews any good", which is the question
that decides whether anyone keeps the bot enabled.

## Status

Documentation only. No code has been written yet — Phase 0 is the next step.

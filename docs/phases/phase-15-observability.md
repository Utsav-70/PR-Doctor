# Phase 15 — Observability

## Goal

Answer "why did this review take eight minutes" and "why did it say that" in under a
minute, from a review ID.

## Depends on

Phase 14. **Unlocks** Phase 16.

## The question to design for

```
Why did review of PR #182 take 8 minutes?

PR #182 · 2m 14s total
 │
 ├── GitHub API        300ms
 ├── Checkout          1.2s
 ├── Context build     12s
 ├── Planner           4s
 ├── Bug Agent         32s
 ├── Security Agent    48s
 ├── Performance       27s
 ├── Semgrep           8s
 ├── Pytest            2m 10s     ◀── there it is
 └── Judge             15s
```

Every stage timed, nested, attributable, reachable from the review ID. If a trace cannot
produce that breakdown, the phase is not done.

## Scope

**In:** OpenTelemetry tracing, Prometheus metrics, Langfuse for LLM traces, Grafana
dashboards, structured logging with correlation IDs, alerts.

**Out:** A bespoke web UI. Grafana is the interface.

## Tracing — OpenTelemetry

One trace per review, spanning the API request and the worker task. The `trace_id` is
stored on the `reviews` row, which is what makes "give me the trace for review X"
answerable.

Context propagation across the queue boundary is the part that needs deliberate work:
inject the trace context into the Celery message headers at enqueue, extract it in the
task. Without this you get two disconnected traces and lose the webhook-to-completion
picture.

Span hierarchy:

```
review (root)
├── webhook.receive
│   ├── webhook.verify_signature
│   └── db.upsert_review
├── github.fetch_pr
├── github.fetch_diff
├── repo.checkout
├── pr.analyze
├── context.build
│   └── tool.{name}          (one span per tool call)
├── agent.planner
├── agent.bug
│   ├── llm.call             (model, tokens, cost, cache hits as attributes)
│   └── tool.{name}
├── agent.security
├── agent.performance
├── evidence.verify
│   ├── semgrep.run
│   ├── ruff.run
│   └── sandbox.run_tests
├── agent.judge
└── github.post_review
```

Attributes worth setting, because they turn a trace into a filterable dataset:

| Span | Attributes |
|---|---|
| `review` | `review_id`, `repository`, `pr_number`, `head_sha`, `status`, `is_partial` |
| `llm.call` | `model`, `stage`, `effort`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_usd`, `stop_reason` |
| `tool.*` | `tool`, `agent`, `allowed`, `result_bytes` |
| `agent.*` | `agent`, `findings_count`, `refused` |
| `sandbox.run_tests` | `exit_code`, `timed_out`, `oom_killed` |
| `github.*` | `status_code`, `rate_limit_remaining` |

Sample at 100%. Review volume is low (tens to hundreds per day), traces are the primary
debugging tool, and sampling loses exactly the rare slow review you need to look at.

## Metrics — Prometheus

Keep the cardinality controlled: label by `repository` and `stage`, never by `pr_number`
or `review_id`. A per-review label turns a metric into a memory leak.

### Counters

| Metric | Labels |
|---|---|
| `prguard_reviews_total` | `status`, `repository` |
| `prguard_findings_total` | `agent`, `category`, `severity` |
| `prguard_findings_rejected_total` | `rejection_code` |
| `prguard_findings_posted_total` | `repository`, `severity` |
| `prguard_llm_calls_total` | `model`, `stage`, `stop_reason` |
| `prguard_llm_refusals_total` | `stage` |
| `prguard_tool_calls_total` | `tool`, `agent`, `allowed` |
| `prguard_webhook_events_total` | `event`, `action`, `outcome` |
| `prguard_dead_letters_total` | `task` |
| `prguard_circuit_breaker_transitions_total` | `breaker`, `state` |

### Histograms

| Metric | Notes |
|---|---|
| `prguard_review_duration_seconds` | Buckets to 1800 s — the tail is the interesting part |
| `prguard_stage_duration_seconds{stage}` | The per-stage breakdown |
| `prguard_llm_latency_seconds{model,stage}` | |
| `prguard_llm_tokens{model,stage,kind}` | `kind` ∈ input, output, cache_read, cache_creation |
| `prguard_review_cost_usd` | |
| `prguard_findings_per_review` | |
| `prguard_diff_size_lines` | Correlate cost and latency against input size |

### Gauges

| Metric | Notes |
|---|---|
| `prguard_queue_depth{queue}` | The leading indicator of everything |
| `prguard_active_reviews` | |
| `prguard_circuit_breaker_state{breaker}` | 0 closed, 1 half-open, 2 open |
| `prguard_daily_cost_usd{installation}` | Against the cap |
| `prguard_cache_hit_ratio{stage}` | |

## LLM tracing — Langfuse

OpenTelemetry gives timings; Langfuse gives the prompts. When a comment is wrong, the
question is "what did the model actually see" — which needs the rendered prompt, the tool
calls, and the raw response.

Per LLM call, record: the rendered prompt (system and messages), tool definitions, the
response, token usage, cost, latency, model, effort, stop reason, and the prompt version.
Group by `review_id` as the trace, `stage` as the span name.

Two things to be careful about:

- **Prompts contain repository source code.** For a private repository that is
  proprietary code leaving your infrastructure. Self-host Langfuse, or disable it in
  production and rely on it in dev and staging only. `LANGFUSE_ENABLED` defaults to
  `false` for this reason.
- **Redact before export.** The Phase 12 secret scrubber runs on the trace payload too.
  Secrets should already be redacted in prompts, but a trace exporter is a second
  egress path and needs the same treatment.

Set `LLM_THINKING_DISPLAY=summarized` when debugging a specific bad review — thinking
summaries are often exactly what explains a strange finding. It changes visibility only,
never billing.

## Structured logging

JSON lines via structlog. Every log line in a review carries `review_id`, `trace_id`,
`repository`, and `pr_number`, bound once at task start via a context var so no call site
has to remember.

Levels, used consistently:

| Level | For |
|---|---|
| DEBUG | Tool calls, GitHub requests, cache decisions |
| INFO | Stage transitions, findings accepted/rejected with reasons, posted comments |
| WARNING | Guardrail drops, retries, refusals, partial reviews, injection patterns detected |
| ERROR | Stage failures, DLQ writes, breaker transitions |
| CRITICAL | Secret detected in output, sandbox escape indicator, App auth failure |

Never log: installation tokens, the App private key, secret values, or full file contents.
Add a structlog processor that redacts known secret shapes as a backstop — relying on
every call site to remember is how tokens end up in logs.

## Dashboards

**Operations** — queue depth, review throughput, p50/p95/p99 duration, error rate by
stage, DLQ depth, breaker states, GitHub rate-limit headroom.

**Quality** — findings per review, accept rate, rejection-code distribution over time,
posted comments per review, verified vs unverified ratio, per-agent contribution.

The rejection-code distribution is the single most informative quality panel. Its shape
tells you which upstream phase is drifting — see the table in
[Phase 8](phase-08-judge.md).

**Cost** — cost per review (p50/p95), daily cost per installation against cap, cost per
posted comment, cache hit rate by stage, token split by kind.

**Latency** — the stacked per-stage breakdown that answers the opening question, plus
duration against diff size.

## Alerts

Alert on symptoms users feel, not on every anomaly. An alert nobody acts on trains people
to ignore the ones that matter.

| Alert | Condition | Severity |
|---|---|---|
| Reviews failing | error rate > 10% over 15 min | page |
| Queue backing up | depth > 50 for 10 min | page |
| Daily cost approaching cap | > 80% of `MAX_COST_PER_DAY_USD` | page |
| Circuit breaker open | any breaker open > 5 min | page |
| Secret detected in output | any occurrence | page |
| DLQ non-empty | depth > 0 for 30 min | ticket |
| Cache hit rate collapsed | reviewer-stage ratio < 0.3 for 1 h | ticket |
| p99 review duration | > 10 min for 30 min | ticket |
| Accept rate anomaly | outside 5–40% over 24 h | ticket |
| GitHub rate limit low | remaining < 500 | ticket |

The cache-hit-rate alert catches a specific and expensive regression: someone adds an
interpolated value to a cached prefix, nothing breaks, and the bill quietly rises 70%.

## Exit criteria

- [ ] One trace per review, spanning API and worker, with the full span tree
- [ ] `reviews.trace_id` links a database row to its trace
- [ ] The per-stage latency breakdown is reproducible for any review ID
- [ ] All listed metrics exported, with bounded label cardinality
- [ ] Four Grafana dashboards live: operations, quality, cost, latency
- [ ] Every log line in a review carries the correlation IDs
- [ ] The redaction processor is active and verified against a known secret
- [ ] Langfuse captures prompts and responses in dev; disabled or self-hosted in prod
- [ ] All ten alerts configured and each verified to fire
- [ ] "Why did review X take N minutes" answerable in under a minute

## Risks

**Losing the trace at the queue boundary.** The default outcome without explicit context
propagation, and it removes the phase's main value.

**Unbounded metric labels.** `pr_number` as a label is a slow memory leak in Prometheus.

**Shipping private source code to a hosted LLM-trace service.** Self-host or keep it to
non-production. Default it off.

**Alert fatigue.** Ten well-chosen alerts beat forty. Everything else is a dashboard.

**Sampling traces.** At this volume, full sampling is affordable and the alternative loses
the rare slow reviews that are the only reason to have tracing.

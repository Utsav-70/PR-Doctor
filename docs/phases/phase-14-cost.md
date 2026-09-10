# Phase 14 — Cost Optimization

## Goal

Route every LLM call through one gateway, account for every token, and enforce hard caps
per review and per day.

## Depends on

Phase 13. **Unlocks** Phase 15.

## Depends on measurement

Read this before optimising: cost changes that trade capability for money are only safe
once **Phase 16** can tell you what they cost in precision. This phase builds the gateway,
the accounting, and the caps — all of which are pure wins. It also builds the *router*,
but the router's aggressive settings should be validated against the evaluation dataset
before being turned on in production. Cheaper and worse is not an optimisation.

## Scope

**In:** the LLM gateway, the model router, token and cost accounting, per-review and
per-day caps, cost attribution.

**Out:** Prompt caching, which was designed into Phase 5 and delivered in Phase 6 — see
[04-llm-strategy.md](../04-llm-strategy.md). It is the largest cost lever in the system
and it is already spent.

## Architecture

```
Agent
  │
  ▼
LLM Gateway
  ├── model router
  ├── cache configuration
  ├── retries and timeouts
  ├── token and cost accounting
  ├── budget enforcement
  └── trace export (Phase 15)
  │
  ▼
Model Router
 ┌┴─────────────┐
 ▼              ▼
Cheap          Strong
Model          Model
```

No agent constructs an Anthropic client. One gateway, so that caps, accounting, retries,
and tracing exist in exactly one place and cannot be bypassed by a new agent added later.

## The router

The user-specified shape:

| Work | Model |
|---|---|
| Simple classification | Cheap model |
| Complex code reasoning | Strong model |
| Judge | Strong model |

Concretely, with a strong bias toward the strong model:

| Stage | Model | Effort | Rationale |
|---|---|---|---|
| File classification fallback | `claude-haiku-4-5` | `low` | Extension-heuristic tiebreak. Mechanical. |
| Title normalisation for dedupe | `claude-haiku-4-5` | `low` | String work |
| Planner | `claude-opus-5` | `medium` | Routing, but a wrong skip costs a whole agent's coverage |
| Bug / security / performance | `claude-opus-5` | `high` | The precision-critical path |
| Evidence agent | `claude-opus-5` | `medium` | Interpreting tool output |
| Judge | `claude-opus-5` | `high` | Final arbiter |

**Everything that reasons about code stays on `claude-opus-5`.** The cheap model handles
work with no judgment in it. This is a deliberate choice, not an oversight: precision is
the metric the product lives or dies by ([00-overview.md](../00-overview.md)), code review
is exactly the workload where model capability converts into precision, and a $0.20 saving
per review that costs one extra false positive per week is a bad trade.

**`effort` is the real dial, not the model.** On `claude-opus-5`, `low` and `medium` are
unusually strong — often matching a weaker model's best output at a fraction of the
tokens. Stepping the evidence agent from `high` to `medium` is a safer economy than
moving it to Sonnet, because it keeps the capability and reduces the spend. Sweep effort
per stage against the Phase 16 dataset; that sweep is where the savings actually are.

If cost pressure demands more, the order of concessions is: lower `effort` per stage →
skip more agents in the planner → tighten context budgets → *then*, and only with
evaluation evidence, consider `claude-sonnet-5` for a specific stage. Never silently
downgrade a reviewer.

Routing is config, not code (`LLM_STRONG_MODEL`, `LLM_CHEAP_MODEL`, per-stage effort
variables), so an operator can adjust it without a deploy — and so Phase 16 can sweep it.

## Accounting

Every call writes an `llm_calls` row:

```python
usage = response.usage
cost = (
    usage.input_tokens * price.input
    + usage.output_tokens * price.output
    + usage.cache_creation_input_tokens * price.input * 1.25
    + usage.cache_read_input_tokens * price.input * 0.10
) / 1_000_000
```

Prices as of the current catalogue ([04-llm-strategy.md](../04-llm-strategy.md)), per
million tokens:

| Model | Input | Output |
|---|---|---|
| `claude-opus-5` | $5.00 | $25.00 |
| `claude-sonnet-5` | $3.00 | $15.00 |
| `claude-haiku-4-5` | $1.00 | $5.00 |

Cache writes bill at ~1.25× input, cache reads at ~0.1×. Both must be in the formula —
omitting cache-creation tokens under-reports the first review of every repository, which
is exactly where cost spikes.

Note that `usage.input_tokens` is only the **uncached remainder**. Total prompt size is
`input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. A dashboard
plotting `input_tokens` alone will show a caching improvement as a mysterious drop in
prompt size.

Keep prices in one versioned table with effective dates, not scattered constants — and
recalculate historical cost with the price in effect at the time, so cost history stays
comparable across a price change.

## Metrics to track

| Metric | Why |
|---|---|
| Tokens per review, split by stage and cache category | Where the spend is |
| LLM calls per review | Detects agent loops |
| Cost per review (p50, p95, p99) | The headline number; p99 is the one that surprises |
| Cost per day, per installation | Billing and abuse detection |
| Cost per *posted comment* | The number that actually matters |
| Cache hit rate per stage | The largest lever's health |
| Cost per finding accepted vs rejected | How much is spent on findings nobody sees |

**Cost per posted comment** is the honest efficiency metric. A review costing $0.50 that
posts three real findings is excellent value. A review costing $0.20 that posts nothing
useful is expensive at any price. Track it per repository — a repository with a very high
cost-per-comment is either exceptionally clean (fine) or being reviewed badly (not).

## Caps

Enforced in the gateway, checked **before** each call, not after:

| Cap | Default | Behaviour on breach |
|---|---|---|
| `MAX_COST_PER_REVIEW_USD` | $1.50 | Stop, complete as `partial`, reason `cost_cap` |
| `MAX_LLM_CALLS_PER_REVIEW` | 25 | Stop, `partial` |
| `MAX_TOKENS_PER_REVIEW` | 500000 | Stop, `partial` |
| `MAX_COST_PER_DAY_USD` | $200 | Circuit breaker; new reviews `skipped` |

Checking before the call matters: a cap checked afterwards has already spent the money.
Estimate the call's input cost from `count_tokens` and refuse if it would breach.

Graceful degradation, in order, when a review approaches its cap:

1. Skip agents the planner ranked lowest.
2. Reduce `effort` for remaining stages.
3. Trim the context bundle.
4. Skip evidence verification for `MEDIUM` and below.
5. Judge whatever exists and post it, marked `partial`.

Never skip the judge. A review that runs out of budget posts a smaller set of judged
findings — never an unjudged one.

The daily cap is per installation, so one repository's runaway cannot exhaust the budget
for everyone. Alert at 80%, not at 100%.

## Reducing cost without reducing capability

Ranked by value:

1. **Prompt caching.** Already built (Phase 6). Verify it — a cache hit rate near zero
   costs ~70% extra on every multi-agent review. This is the whole ballgame. With no
   test suite, the Phase 15 cache-hit-rate alert is what catches a regression here.
2. **Skip agents earlier.** The planner declining to run the performance agent on a
   docs-and-config PR saves 100% of that agent's cost. Free.
3. **Tighter context bundles.** Symbol signatures instead of bodies; capped reference
   lists; no test bodies unless directly named. Often improves precision too, by reducing
   noise.
4. **`effort` sweeps per stage.** The main dial. Requires Phase 16 to do safely.
5. **Deterministic-first ordering.** Run Semgrep, Ruff, and Bandit *before* the reviewers
   and include their findings in the context. Cheap signal that focuses expensive
   attention. Worth doing.
6. **Cache tool results within a review.** Three agents asking for the same symbol should
   pay once.
7. **Batch evidence verification by file.** Already specified in Phase 7.
8. **Skip verification below the posting threshold.** Findings that will never be posted
   should never be verified.

Explicitly *not* on this list: shortening the system prompts to save input tokens. They
sit behind a cache breakpoint and are read at 0.1×; the saving is negligible and the
precision cost is not.

## Exit criteria

- [ ] Every LLM call goes through the gateway; no agent constructs a client
- [ ] Every call writes an `llm_calls` row with accurate tokens and cost
- [ ] Cache creation and read tokens are both included in the cost formula
- [ ] Cost per review, per day, and per installation are queryable
- [ ] Cost per posted comment is tracked per repository
- [ ] All four caps enforced before the call, not after
- [ ] Degradation order implemented; the judge always runs
- [ ] Routing is config-driven and adjustable without a deploy
- [ ] Cache hit rate per stage is visible and non-zero on the reviewer stages
- [ ] Cost for the sample PR recorded, and re-checked after every prompt change
- [ ] Measured cost per review documented as the Phase 16 baseline

## Risks

**Optimising before measuring.** Every capability-for-cost trade made before Phase 16 is
an unmeasured bet. Build the gateway and the caps now; make the trades after.

**Downgrading the reviewers to save money.** The most tempting and most damaging change
available. Precision is the product.

**A cap checked after the call.** The money is already spent.

**Ignoring cache-creation tokens.** Under-reports every cold-cache review and hides the
cost of any change that invalidates the prefix.

**Optimising `input_tokens` instead of total prompt size.** Caching moves tokens between
usage fields; a dashboard watching one field will read a cache regression as an
improvement.

**Not alerting until the cap is hit.** By then reviews are already being skipped. Alert
at 80%.

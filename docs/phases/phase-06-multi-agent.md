# Phase 6 — Multi-Agent Architecture

## Goal

Split the single reviewer into a planner and three specialists that run in parallel over
one shared context bundle.

## Depends on

Phase 5. **Unlocks** Phase 7.

## Why split at all

A generalist reviewer spreads attention. Given a diff touching payments, it will notice
the duplicate-charge risk *or* the unparameterised query *or* the N+1 — rarely all three,
because each requires holding a different model of what can go wrong. Three specialists
with narrow prompts each cover their domain properly.

The cost objection is answered by prompt caching: the three agents share the repository
context bundle behind one cache breakpoint, so agents two and three pay ~0.1× for the
expensive part of their input. Without that, this phase would roughly triple the bill;
with it, the increase is modest. Verify it — see the cost table in
[04-llm-strategy.md](../04-llm-strategy.md).

## Scope

**In:** planner node, three specialist agents, parallel fan-out, merge, per-agent
attribution, per-agent tool permissions.

**Out:** Verification and deduplication across agents — findings from this phase are still
raw candidates. Phase 7 verifies, Phase 8 dedupes and judges.

## The graph

```
                    START
                      │
                      ▼
                  Fetch PR
                      │
                      ▼
                Build Context      ← one bundle, one cache breakpoint
                      │
                      ▼
                   Planner
                      │
          ┌───────────┼────────────┐
          ▼           ▼            ▼
       Bug Agent  Security     Performance
          │        Agent         Agent
          └───────────┼────────────┘
                      ▼
                All Findings       ← merge, attribute, no filtering yet
                      │
                      ▼
               Validate Output
                      │
                      ▼
                     END
```

Fan-out is LangGraph parallel edges driven by `asyncio.gather`, inside the one Celery
task. Not sub-tasks: the review stays a single atomic unit with one timeout, one trace,
and one cost total.

## The planner

Cheap routing, not analysis. **Model:** `claude-opus-5`, **effort:** `medium`.

Given the file list, classifications, and a digest of the diff, it decides:

- **Which agents to run.** A docs-only PR needs none. A dependency-manifest change needs
  security and nothing else. A pure test addition needs bug only. Skipping an agent is
  the cheapest possible optimisation and the planner's main job.
- **Per-agent focus hints.** Which files each agent should prioritise — the security agent
  gets `auth.py` and `payment/` first; the performance agent gets the file with the new
  loop.
- **Whether extra context is needed** before fan-out — e.g. resolve the callers of a
  changed public function once, centrally, rather than three agents each discovering the
  need.

```python
class ReviewPlan(BaseModel):
    run_agents: list[Literal["bug", "security", "performance"]]
    focus: dict[str, list[str]]  # agent -> ordered file paths
    extra_context: list[ContextRequest]
    rationale: str  # recorded, not posted
```

Deterministic guardrails around the plan, because a planner that decides to skip the
security agent on `auth.py` is a bug with consequences:

- Security agent is **always** run when any file matches the security-sensitive path
  hints or is a dependency manifest, regardless of what the planner said.
- Bug agent is always run when any `source` file changed.
- `ENABLED_AGENTS` config is a hard filter applied after the plan.

The planner narrows; it never overrides a floor.

## The three agents

All three: `claude-opus-5`, effort `high`, adaptive thinking, the same `FindingsReport`
schema from Phase 5, the same shared context bundle. What differs is the system prompt,
the tool permissions, and the `agent` field stamped on every finding.

### Bug agent

Correctness. `agent/prompts/bug.md`.

- **Logic errors** — inverted conditions, wrong operators, off-by-one, incorrect boolean
  composition.
- **Edge cases** — empty collections, zero, negative numbers, `None`, unicode, boundary
  values, single-element cases.
- **Exception paths** — unhandled exceptions, over-broad `except`, swallowed errors,
  cleanup that does not run on the error path, resources not released.
- **Incorrect state** — mutation of shared or default-argument state, stale caches,
  invariants broken between two statements, partial updates on failure.
- **Race conditions** — check-then-act on shared state, non-atomic read-modify-write,
  missing locks, unsafe lazy initialisation, TOCTOU.
- **Idempotency** — retries that duplicate a side effect. The duplicate-payment class of
  bug lives here.

Tools: all five read tools. `find_references` and `get_tests` matter most — "this can be
called with `None`" needs a caller that does, and "this path is untested" needs the test
search.

### Security agent

`agent/prompts/security.md`.

- **Injection** — SQL (string interpolation or concatenation into a query, `.format()`,
  f-strings), command injection (`shell=True`, `os.system`), template injection, LDAP,
  NoSQL.
- **XSS** — unescaped output, `|safe`, `dangerouslySetInnerHTML`, `mark_safe`.
- **SSRF** — user-controlled URLs reaching an HTTP client; missing allowlists; redirect
  following.
- **Auth and authorization** — missing checks on a new endpoint, IDOR (an object fetched
  by ID with no ownership check), privilege escalation, wrong decorator, auth checked
  after the side effect.
- **Secrets** — hardcoded keys, tokens, passwords, connection strings; secrets logged;
  secrets in error messages.
- **Unsafe deserialization** — `pickle`, `yaml.load` without `SafeLoader`, `eval`,
  `exec`, `marshal`.
- **Crypto misuse** — MD5/SHA1 for security purposes, ECB mode, static IVs, `random`
  instead of `secrets`, missing constant-time comparison.
- **Dependencies** — a new dependency, a downgrade, or a pin change on a manifest.

Tools: all five. The characteristic security question is a dataflow one — where does this
value come from — so `find_references` gets heavy use. Phase 7 turns those traces into
evidence and Phase 10 corroborates with Semgrep and Bandit.

### Performance agent

`agent/prompts/performance.md`.

- **N+1 queries** — a query inside a loop; a lazy relation accessed per iteration; a
  missing `select_related` / `join` / batch fetch.
- **Unnecessary loops** — nested iteration where a set or dict lookup would do; repeated
  work that could be hoisted; list scans in a hot path.
- **Memory** — loading an unbounded result set into a list, reading a whole file when
  streaming would do, an unbounded cache or accumulator.
- **Blocking operations** — sync IO in an async function (the classic: `requests` inside
  `async def`), a blocking call on the event loop, an unbounded `await` without timeout.
- **Algorithmic complexity** — an O(n²) introduced where O(n) existed, sorting inside a
  loop, a regex compiled per call.

Tools: `read_file`, `get_symbol`, `find_references`. **Not** `search_code` — the
performance agent's questions are local and structural, and denying it a broad text search
keeps it from wandering. Denials are recorded, so if this turns out to be wrong the audit
log will say so.

Performance findings need care on severity. An O(n²) over a list that is provably length
3 is not a finding. The prompt says so explicitly, and asks for the finding to state the
scale at which it matters.

## Merge

Concatenate, stamp `agent`, run the Phase 5 validation on the union. **No filtering, no
deduplication.** Two agents independently reporting the same line is signal, not noise —
Phase 8 treats agreement as a confidence boost and only then collapses the duplicate.

Per-agent failure isolation: a failed or refused agent appends to `state["errors"]` and
the merge proceeds with the others. A review missing one agent's coverage is recorded as
such and still worth posting; a review that fails because one of three prompts hit a
refusal is not.

Cap findings per agent (`MAX_FINDINGS_PER_AGENT`, default 20) so one agent cannot flood
the judge's input.

## Cost

Verify the caching claim empirically in this phase, because it is the assumption the
architecture rests on:

- Agent 1 pays `cache_creation_input_tokens` on the context breakpoint.
- Agents 2 and 3 should show `cache_read_input_tokens` of roughly the same magnitude and
  near-zero `input_tokens` for that segment.

If agents 2 and 3 show full `input_tokens`, the bundle is being rendered per-agent —
usually because a focus hint or agent name was interpolated into the cached block instead
of the trailing uncached one. Focus hints belong in the **final, uncached** message block.

## Exit criteria

- [ ] Planner selects agents and its choices are recorded with rationale
- [ ] Deterministic floors override the planner where specified
- [ ] Three agents run in parallel; wall clock is close to the slowest, not the sum
- [ ] Every finding carries its `agent`
- [ ] A planted SQL injection is found by the security agent
- [ ] A planted N+1 is found by the performance agent
- [ ] A planted off-by-one is found by the bug agent
- [ ] One agent failing does not fail the review
- [ ] Cache-read tokens confirm the shared context breakpoint is working
- [ ] Cost per review is measured and compared against the Phase 5 baseline
- [ ] Duplicates from two agents survive to Phase 8 rather than being merged here

## Risks

**Cache prefix divergence between agents.** The one mistake that makes this phase
expensive instead of cheap. Test rendered bytes, not behaviour.

**Deduplicating in the merge.** Discards the strongest confidence signal available —
independent agreement. Leave it to the judge.

**A planner that skips too much.** Every skipped agent is a recall loss that no metric
catches until Phase 16. Keep the deterministic floors, and log every skip.

**Three prompts drifting apart.** Shared structure (scope rules, citation rules, coverage-
not-filtering, length limits) belongs in one included fragment, with only the domain
section differing per agent. Otherwise a fix applied to one prompt silently misses the
other two.

**Performance-agent false positives.** The domain most prone to plausible-but-irrelevant
findings, because complexity claims sound rigorous. Requiring a stated scale in the
description is a cheap and effective filter.

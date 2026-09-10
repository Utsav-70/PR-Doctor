# Phase 10 — Static Analysis

## Goal

Give the evidence layer real deterministic tooling. Semgrep, Ruff, and Bandit become
evidence sources; agreement between the model and a rule engine becomes the strongest
non-executable signal available.

## Depends on

Phase 9. **Unlocks** Phase 11.

## The principle, restated

> **The LLM proposes; deterministic tooling verifies wherever possible.**

Phase 7 built the verification framework with search and AST checks. This phase supplies
the heavy artillery. It is the point at which "verified" starts meaning something a
sceptic would accept.

Note the direction of the integration: static analysis output is **evidence**, not
comments. PRGuard does not post Ruff findings — teams that want a linter run a linter.
What Ruff provides is corroboration.

## Scope

**In:** Semgrep, Ruff, and Bandit as evidence providers; result parsing and normalisation;
mapping tool findings to agent findings; confidence contribution.

**Out:** Running pytest (Phase 11 — needs the sandbox). Posting tool findings directly.
Note that Semgrep and Bandit only *read* code, so they can run in the worker in this
phase; the sandbox move in Phase 11 is a hardening step, not a prerequisite.

## Architecture

```
                 Finding
                    │
        ┌───────────┼───────────┬──────────────┐
        ▼           ▼           ▼              ▼
       LLM       Semgrep     Ruff/Bandit     Tests
    (proposes)  (verifies)   (verifies)    (Phase 11)
        │           │           │              │
        └───────────┴─────┬─────┴──────────────┘
                          ▼
                      Evidence
```

## The tools

### Semgrep — the primary evidence engine

Semantic pattern matching with real dataflow analysis. This is the tool that can actually
confirm a taint path from a request parameter to a SQL string.

```bash
semgrep --config p/security-audit --config p/python \
        --json --quiet --timeout 120 --max-target-bytes 1000000 \
        --metrics off \
        <changed files only>
```

- **Run on changed files only.** Whole-repository scans take minutes and produce findings
  about code this PR did not touch.
- `--metrics off` — no telemetry from a private repository.
- Registry rulesets (`p/security-audit`, `p/python`) plus a small local rule directory in
  `security/semgrep_rules/` for project-specific patterns.
- Pin the Semgrep version and vendor the rulesets. A registry ruleset silently changing
  between reviews makes precision non-reproducible, which breaks Phase 16.
- Run offline (`--metrics off` plus pre-fetched rules) so a review never depends on
  network access to the registry.

Semgrep gets weight **0.90** — the highest available before executable proof.

### Ruff — cheap structural facts

Fast enough to run on every review with no meaningful cost. Useful evidence categories:

| Ruff rules | Corroborates |
|---|---|
| `B` (bugbear) | Mutable default arguments, unused loop variables, broad excepts |
| `S` (bandit subset) | Overlaps Bandit; use whichever the project already runs |
| `ASYNC` | Blocking calls in async functions — directly confirms a performance finding |
| `E722`, `BLE` | Bare and broad `except` clauses |
| `F` (pyflakes) | Undefined names, unused imports — occasionally a real bug the agent spotted |

```bash
ruff check --output-format json --no-cache <changed files>
```

Weight **0.75**. Noisier than Semgrep and rule-based rather than dataflow-based, but its
`ASYNC` and `B` rules map unusually cleanly onto bug and performance findings.

### Bandit — Python security specifics

Overlaps Semgrep but catches some Python-specific patterns well: `assert` in production
code, `subprocess` with `shell=True`, weak hash usage, `yaml.load`, hardcoded temp paths.

```bash
bandit -f json -q -r <changed files>
```

Weight **0.75**. Where Bandit and Semgrep agree, treat it as one strong signal rather
than two independent ones — they frequently implement the same rule, and double-counting
correlated evidence inflates confidence.

### Pytest — deferred to Phase 11

Test existence is already evidence in Phase 7 (`get_tests`). Test *execution* runs
attacker-supplied code and does not happen until the sandbox exists.

## Mapping tool output to findings

The engineering problem of the phase: Semgrep says "rule `python.lang.security.audit.
formatted-sql-query` at `repo.py:87`"; the agent said "SQL injection at `repo.py:87`".
Connecting those two is what turns tool output into evidence.

Three-tier matching, most specific first:

**1. Rule-to-category mapping.** A curated table from tool rule IDs to PRGuard categories
and finding types:

```yaml
- rule: python.lang.security.audit.formatted-sql-query
  category: SECURITY
  finding_type: sql_injection
  weight: 0.90
- rule: python.django.security.audit.unsafe-mark-safe
  category: SECURITY
  finding_type: xss
- rule: ASYNC101
  category: PERFORMANCE
  finding_type: blocking_in_async
- rule: B006
  category: BUG
  finding_type: mutable_default_arg
```

The table is data in `security/rule_map.yaml`, reviewed and extended by hand. Start with
the twenty or so rules that map to the finding types the agents actually produce; do not
try to map the whole Semgrep registry.

**2. Line proximity.** A tool finding within ±3 lines of the agent's cited line, in the
same file and mapped category, is a match. Same-line is a strong match; nearby is a
weaker one, because a single expression spans lines and the two tools may anchor
differently.

**3. Semantic fallback.** For a tool finding with no mapping and no line match, the
evidence agent (Phase 7) is shown the tool message and asked whether it describes the same
issue. Used sparingly; weight is reduced when the match came from this tier.

Two outcomes need explicit handling:

- **Tool findings with no matching agent finding.** Recorded as unmatched. These are
  recall diagnostics: a Semgrep security hit that no agent reported means the security
  prompt has a gap. Do not post them — the agents' job is to explain, and an unexplained
  rule ID is a linter comment. But do count them in Phase 16.
- **Agent findings on files the tool could not parse.** `inconclusive`, not `refutes`.
  Absence of a Semgrep finding on an unparseable file proves nothing.

## The absence-of-evidence problem

This deserves care because it is where over-confidence creeps in.

Semgrep *not* flagging a line is weak evidence of safety, not proof. Its rules cover
common patterns, not all patterns. So:

| Situation | Outcome |
|---|---|
| Rule exists for this finding type, ran successfully, no hit | `refutes`, weight 0.60 |
| No rule exists for this finding type | `inconclusive` |
| Tool failed, timed out, or could not parse the file | `error` — recorded, contributes nothing |

The first row is the subtle one: it is a genuine but *reduced-weight* refutation. Treating
"Semgrep did not flag it" as a strong refutation silently suppresses real bugs in the long
tail that rules do not cover — which is exactly the tail an LLM reviewer is good at.

## Execution and caching

- Run all three tools **once per review**, in parallel, over the changed file set — not
  once per finding. Twenty findings share one Semgrep run.
- Cache results in Redis keyed on `(tool, tool_version, ruleset_hash, file_content_hash)`.
  Re-reviews of the same file after an unrelated push are then free.
- Hard timeouts per tool (`SEMGREP_TIMEOUT_SECONDS` etc.). On timeout, record `error` and
  continue — a slow tool degrades verification coverage rather than failing the review.
- Run before the evidence agent's interpretation call, so the plan can reference real
  results.

## Exit criteria

- [ ] All three tools run on changed files and their output is parsed into evidence rows
- [ ] Rule map covers the finding types the agents actually produce
- [ ] Tool agreement measurably raises confidence on a sample PR
- [ ] Tool refutation measurably lowers it
- [ ] Unparseable files yield `inconclusive`, never `refutes`
- [ ] Unmatched tool findings are recorded and not posted
- [ ] Correlated tools are not double-counted
- [ ] Tool versions and rulesets are pinned and vendored
- [ ] One tool run per review, cached across re-reviews
- [ ] A tool timeout degrades verification without failing the review

## Risks

**Posting tool findings as comments.** Turns PRGuard into a worse linter than the linter
the team already has, and buries the findings only an LLM could produce.

**Unpinned rulesets.** Precision changes between reviews for reasons nobody can see, and
Phase 16's numbers become meaningless.

**Treating rule silence as proof of safety.** Suppresses exactly the long-tail bugs that
justify having an LLM reviewer at all.

**Double-counting Semgrep and Bandit.** They implement overlapping rules; two correlated
confirmations are not two independent ones.

**Whole-repository scans.** Minutes of latency and a flood of findings about untouched
code.

# Phase 7 — Evidence Verification

## Goal

Before any finding is treated as real, try to prove it with something that cannot
hallucinate. Every candidate finding comes out of this phase with `verified` set and a
confidence adjusted by what the tooling actually showed.

This is the most important phase in the project.

## Depends on

Phase 6. **Unlocks** Phase 8.

## The problem

A reviewer agent reporting "SQL injection possible at line 87" is stating a hypothesis
with a number attached. The number is the model's self-assessment, and self-assessed
confidence is not calibrated — a wrong finding and a right finding both come back at 0.9.

Posting on self-reported confidence alone means posting a steady stream of confident
nonsense. One or two of those per PR and developers stop reading the bot. Since precision
is the metric that decides whether PRGuard is used at all
([00-overview.md](../00-overview.md)), verification is not a refinement — it is the
product.

## The principle

**The LLM proposes; deterministic tooling verifies.**

An agent generates hypotheses, which it is good at. Tools check facts, which they are
good at. The evidence agent's job is not to re-review the code — it is to decide *which
checks would settle this claim*, run them, and report what came back.

## Scope

**In:** the evidence agent, verification strategies per finding category, the evidence
table, confidence adjustment, refutation.

**Out:** Semgrep/Ruff/Bandit integration (Phase 10 — this phase builds the framework with
search, AST, and test-existence checks). Running tests (Phase 11 — needs the sandbox).
Accept/reject decisions (Phase 8).

## Architecture

```
                 Candidate Finding
                        │
                        ▼
                 Evidence Agent
              (chooses the checks)
                        │
          ┌─────────────┼─────────────┬──────────────┐
          ▼             ▼             ▼              ▼
      search_code    AST /        static analysis  get_tests
      (grep facts)  dataflow      (Phase 10)      (coverage)
          │             │             │              │
          └─────────────┴──────┬──────┴──────────────┘
                               ▼
                        evidence rows
                    (supports / refutes /
                     inconclusive / error)
                               │
                               ▼
                    verified + verified_confidence
```

**Model:** `claude-opus-5`, **effort:** `medium`. Lower than the reviewers because the
work is choosing checks and interpreting tool output, not open-ended analysis.

## How a check gets chosen

The evidence agent is given the finding, the relevant code, and the tool set, and returns
a verification plan:

```python
class VerificationPlan(BaseModel):
    checks: list[Check]

    class Check(BaseModel):
        method: Literal["search", "ast", "semgrep", "ruff", "bandit", "tests", "dataflow"]
        rationale: str
        args: dict
```

Then the plan executes deterministically through the tool gateway, and the agent
interprets the results in a second call:

```python
class EvidenceVerdict(BaseModel):
    verified: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    supporting: list[str]  # which checks supported, and what they showed
    refuting: list[str]  # which checks refuted
```

Two calls rather than one, deliberately: a single call that both plans and concludes
lets the model reason its way to a verdict without the tool output constraining it. The
split forces the verdict to come *after* the facts.

## Worked example — a SQL injection claim

The agent says: *"SQL injection possible at `users/repo.py:87`."*

The evidence agent asks the questions a security engineer would:

```
Where does the value originate?
        ↓  find_references on the parameter; trace to the caller
   Is it user-controlled?
        ↓  does it reach a request handler / CLI arg / message payload?
How does it reach the SQL?
        ↓  AST: is the query built by f-string, %, .format(), or concatenation?
Is parameterization used?
        ↓  AST: does the execute() call have a params argument?
Does Semgrep confirm?          (Phase 10)
        ↓  run the sqlalchemy / python.lang.security rulesets on this file
```

Outcomes and what they mean:

| What the checks show | Verdict |
|---|---|
| f-string query + traced to a request parameter + Semgrep hit | `verified: true`, confidence ~0.97 |
| f-string query but the value is a module-level constant | `refutes` — not user-controlled |
| `execute(sql, params)` with placeholders | `refutes` — parameterised; false positive |
| Query built dynamically, origin untraceable | `inconclusive` — real risk, unproven |

```json
{"verified": true, "confidence": 0.97}
```

That number is worth something, because it is downstream of a grep hit at a specific line
and an AST fact about a specific call.

## Strategies by category

### SECURITY

| Claim | Checks |
|---|---|
| SQL injection | AST on the query construction + `find_references` for taint origin + Semgrep |
| Command injection | AST for `shell=True` / `os.system` + argument origin |
| XSS | AST for the escape-bypass call + template context |
| SSRF | `find_references` from the URL parameter to the HTTP client call |
| Missing authz | `get_symbol` on sibling handlers — do comparable endpoints have the check the new one lacks? |
| Hardcoded secret | Gitleaks / regex + entropy on the exact line (Phase 12) |
| Unsafe deserialization | AST for `pickle.loads`, `yaml.load` without `SafeLoader`, `eval` |

The sibling-handler comparison is the strongest available check for authorization
findings and generalises well: *is this file internally inconsistent with itself?*

### BUG

| Claim | Checks |
|---|---|
| Unhandled exception | AST: does an enclosing `try` cover this call? Does the callee raise? |
| `None` dereference | `find_references`: does any caller pass `None` or an `Optional`? |
| Off-by-one | AST on the range/slice bounds + the collection's construction |
| Race condition | AST for lock acquisition around the shared access; is the access shared at all? |
| Missing idempotency | `search_code` for a unique constraint or idempotency key on the entity |
| Untested path | `get_tests` — does any test reach this branch? |

### PERFORMANCE

| Claim | Checks |
|---|---|
| N+1 query | AST: is the query call lexically inside a loop? Is the loop bounded? |
| Sync IO in async | AST: is the call inside an `async def`, and is the callee sync? |
| Unbounded memory | AST: is there a `limit` / `chunk` / slice on the fetch? |
| Complexity regression | AST loop-nesting depth + whether the collection size is knowable |

Performance claims are the easiest to refute mechanically, which is useful: "the loop
runs over a three-element literal" is a definitive `refutes`.

## Evidence weighting

Not all checks are equally trustworthy. Each method carries a weight, applied by the
judge in Phase 8:

| Method | Weight | Why |
|---|---|---|
| `tests` (a failing test) | 1.00 | Executable proof (Phase 11) |
| `semgrep` | 0.90 | Deterministic rule, real dataflow analysis (Phase 10) |
| `ast` | 0.85 | Structural fact about the code |
| `bandit` / `ruff` | 0.75 | Deterministic but noisier, more false positives |
| `dataflow` | 0.70 | Heuristic tracing through `find_references` |
| `search` | 0.60 | A text match proves presence, not semantics |

`find_references` returns `confidence: "heuristic"` from Phase 4 for good reason — it does
no type inference. The weight reflects that honestly rather than laundering it.

## Confidence adjustment

```
verified_confidence = agent_confidence
                    × (1 + Σ supporting_weights × α)
                    × (1 − Σ refuting_weights × β)
```

clamped to `[0, 1]`, with `α` and `β` tuned in Phase 16 rather than guessed. The shape
that matters:

- **Any `refutes` from a high-weight method drives confidence near zero.** A refutation is
  much stronger than a confirmation: proving parameterisation is used definitively kills
  a SQL-injection claim, whereas confirming an f-string only makes it likely.
- **Multiple independent `supports` compound**, but with diminishing returns.
- **All `inconclusive`** leaves confidence where the agent put it and sets
  `verified = false` — held to the higher bar in Phase 8, not rejected outright.
- **A check that errored** contributes nothing in either direction and is recorded so
  Phase 15 can show that verification coverage is degraded.

## Persistence

One `evidence` row per check: `method`, `outcome`, `weight`, `detail` (rule IDs, matched
lines, exit codes, stderr), `duration_ms`. On the finding: `verified` and
`verified_confidence`.

`detail` is what makes a posted comment defensible — Phase 9 can include "no unique
constraint on `payments.idempotency_key`" in the comment body because an evidence row
recorded exactly that.

Keep everything, including refutations. The set of findings that were refuted is the
highest-signal dataset in the system: it is where Phase 16's false-positive analysis
starts, and it is how you tell a prompt regression from a tooling regression.

## Cost and time control

Verification multiplies calls: twenty candidate findings times two calls each is forty
calls before the judge has run. Controls:

- **Batch by file.** All findings in one file are verified in one planning call — they
  share context, and the context is already cached.
- **Skip verification for `INFO` and `LOW`** severity findings below the posting
  threshold. They are recorded, never posted, and not worth verifying.
- **Cap total verification calls** per review; when the cap is hit, remaining findings
  stay `verified = null` and the judge applies the unverified bar.
- **Cache check results** within a review keyed on `(method, args)` — three agents
  reporting on the same line will produce overlapping plans.

## Exit criteria

- [ ] Every finding leaves this phase with `verified` and `verified_confidence` set
- [ ] Each finding has ≥1 `evidence` row, or an explicit `inconclusive` with a reason
- [ ] A parameterised query reported as injection is refuted with `refutes` evidence
- [ ] A loop over a 3-element literal reported as N+1 is refuted
- [ ] A real f-string SQL query traced to user input is verified above 0.9
- [ ] Refuted findings are retained in the database with their evidence
- [ ] Evidence `detail` carries enough for a comment to cite it
- [ ] Verification-call count and cost stay within caps
- [ ] Batching means N findings in one file cost one planning call

## Risks

**Confirmation bias in a single call.** If the same call both plans and concludes, the
model reasons toward the finding it was given. The two-call split exists to prevent this,
and it is the phase's most important structural decision.

**Treating `inconclusive` as `refutes`.** Silently discards real bugs that happen to be
hard to prove. Inconclusive means "unproven", and Phase 8 decides what to do about it.

**Treating a grep hit as proof.** A text match for `execute(` near an f-string is
suggestive, not conclusive. That is what the weights are for — do not flatten them.

**Verification cost exceeding review cost.** Batch by file, skip below-threshold
severities, and cap. Without those three, verification is the most expensive stage in the
pipeline.

**No refutation path.** An evidence layer that can only confirm is not verification, it
is agreement. Every strategy needs a case where it says no — and a test proving it does.

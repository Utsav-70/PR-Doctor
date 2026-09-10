# Phase 8 — Judge Agent

## Goal

One component decides what gets posted. Everything upstream produces candidates; the
judge produces the final set, with a recorded reason for every acceptance and every
rejection.

## Depends on

Phase 7. **Unlocks** Phase 9.

## Why a separate judge

The reviewer agents are told to report everything and filter nothing
([Phase 6](phase-06-multi-agent.md)) — a reviewer that self-censors finds bugs and stays
quiet about them. That decision only works if something downstream does the filtering.

The judge also sees things no individual agent can: that two agents reported the same
line, that the security agent's finding contradicts the bug agent's, that this exact
finding was posted on the previous commit of this PR, that fifteen findings is more than
anyone will read.

## Scope

**In:** the judge agent, deduplication, rejection taxonomy, final confidence, severity
normalisation, ranking, output cap.

**Out:** Posting and the mechanical output guardrails (Phase 9 — the judge decides
*whether*, the guardrails check *that it is safe to*).

## Flow

```
3 agents
   │
   ▼
~20 candidate findings
   │
   ▼
Evidence verification         (Phase 7)
   │
   ▼
   Judge
   │
   ├── duplicate         → reject
   ├── weak evidence     → reject
   ├── irrelevant        → reject
   ├── false positive    → reject
   ├── below threshold   → reject
   │
   └── valid             → accept, rank, cap
   │
   ▼
2–5 findings posted
```

Twenty candidates down to a handful is the expected ratio, not a sign something is
broken. A judge that accepts most of its input is not judging.

## The agent

**Model:** `claude-opus-5`, **effort:** `high`. The most consequential single call in the
pipeline — a wrong rejection loses a real bug, a wrong acceptance costs trust. Do not
economise here.

Input per finding: the finding itself, every evidence row with its outcome and weight,
the code at the cited line, which agent reported it, and the set of other findings in
this review (for duplicate and contradiction detection).

Output:

```python
class Judgment(BaseModel):
    finding_id: str
    accepted: bool
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(description="Why accepted or rejected, one or two sentences")
    rejection_code: (
        Literal["duplicate", "weak_evidence", "irrelevant", "false_positive", "below_threshold"]
        | None
    )
    revised_description: str | None = Field(
        description="Rewritten description if the original was unclear or overstated"
    )


class JudgeVerdict(BaseModel):
    judgments: list[Judgment]
```

Judging happens in **one call for the whole review**, not per finding. Cross-finding
decisions — duplicates, contradictions, overall volume — are only possible with the full
set in view.

`revised_description` matters more than it looks: reviewer descriptions are sometimes
correct but overstated ("this will corrupt the database" for something that raises a
handled exception). The judge can accept the finding and tone it down. Severity is also
re-set here, so the posted severity is a considered one rather than the reporting agent's
first instinct.

## Rejection reasons

Each has a definition tight enough to be testable.

### `duplicate`

The same underlying issue reported more than once. Detection is two-stage:

1. **Deterministic pre-pass** on `dedupe_key` = hash of
   `(file, line, category, normalised title)`. Exact collisions collapse before the LLM
   call, keeping the input smaller and the decision cheaper.
2. **Semantic pass** by the judge for near-duplicates — the same bug reported at two
   lines of the same expression, or the same issue framed as `BUG` by one agent and
   `RELIABILITY` by another.

**Independent agreement raises confidence.** When two agents find the same thing, keep
one finding, credit both, and increase confidence — that is the strongest signal
available short of a failing test. Merge, do not discard.

Also dedupe **against previous reviews of the same PR**: on a `synchronize` event, a
finding already posted against an earlier commit, on a line the new commit did not touch,
is a duplicate. Without this, a PR with five pushes accumulates five copies of every
comment. Query prior `findings` rows for the same `(repository_id, pr_number)` with
`posted_at IS NOT NULL`.

### `weak_evidence`

No `supports` evidence, or only low-weight support, on a claim whose category has a
strong check available. A SQL-injection finding that Semgrep did not corroborate and no
AST check confirmed is weak; the same finding on a file Semgrep could not parse is
`inconclusive`, which is different.

Two bars, set by `REQUIRE_EVIDENCE`:

- **Verified** findings: accepted at `confidence ≥ CONFIDENCE_THRESHOLD` (default 0.85).
- **Unverified** findings: need `confidence ≥ threshold + 0.10` **and** a severity of
  `HIGH` or above. A `MEDIUM` unverified finding is not worth the trust it risks.

### `irrelevant`

Real, but not worth saying. The judge is explicitly instructed to reject:

- Style, naming, formatting, and import-ordering opinions.
- Pre-existing issues on lines this PR did not change.
- Test-file findings about test style rather than test correctness.
- Complexity claims on provably-small collections.
- Defensive-programming suggestions for states that cannot occur.
- Anything a linter already reports (Ruff findings surface as evidence in Phase 10, not
  as comments).

### `false_positive`

The evidence refutes it. Usually mechanical — a high-weight `refutes` row drives
confidence near zero in Phase 7 and the judge codifies it. Kept as a distinct code from
`weak_evidence` because the two mean different things for diagnosis: `false_positive`
means the reviewer was wrong, `weak_evidence` means we could not tell.

### `below_threshold`

Passed everything else but landed under `CONFIDENCE_THRESHOLD` or under
`MIN_SEVERITY_TO_POST`. A pure config outcome, separated from the judgment codes so that
threshold tuning in Phase 16 is visible as its own line in the rejection breakdown.

## Ranking and the cap

Accepted findings are ordered by severity, then confidence, then file path (stable). The
top `MAX_COMMENTS_PER_REVIEW` (default 15) are posted; the rest are recorded with
`rejection_code = "below_threshold"` and noted in the summary as *"N further lower-
severity findings not shown."*

Fifteen is generous. In practice a good review posts two to five comments. If reviews
routinely hit the cap, the threshold is too low — that is a signal to act on, not a
volume to accept.

## Determinism guardrails

The judge is an LLM and can be wrong, so a thin deterministic layer surrounds it:

- **Never accept a finding the judge did not judge.** A finding missing from
  `JudgeVerdict` is rejected by default, not accepted by omission.
- **Never accept a finding whose evidence contains a high-weight `refutes`**, whatever the
  judge said. Log the disagreement — it is a prompt bug worth knowing about.
- **Never accept below `CONFIDENCE_THRESHOLD`.** The threshold is config, not advice.
- **If the judge call fails or refuses, post nothing** and mark the review `failed`. The
  invariant from [01-architecture.md](../01-architecture.md): a broken PRGuard posts
  nothing, not something wrong. There is no "post unjudged findings" fallback, ever.

## Persistence

On each finding: `judge_accepted`, `judge_confidence`, `judge_reason`, `rejection_code`,
and the final `severity`. `revised_description` overwrites `description` — with the
original kept in a `description_original` column, because comparing the two across a
hundred reviews tells you exactly how the reviewer prompts are miscalibrated.

The rejection-code distribution is the primary health metric of the whole pipeline:

| Pattern | What it means |
|---|---|
| `duplicate` dominant | Agent prompts overlap too much, or PR-level dedupe is missing |
| `weak_evidence` dominant | Phase 7 coverage is thin — more strategies needed |
| `irrelevant` dominant | Reviewer prompts have the wrong bar for what counts |
| `false_positive` dominant | Reviewer prompts or the model are over-firing |
| `below_threshold` dominant | Threshold may be miscalibrated — check against Phase 16 |
| Nothing rejected | The judge is not judging |

Graph it per repository in Phase 15.

## Exit criteria

- [ ] Every finding has `judge_accepted` set and, when rejected, a `rejection_code`
- [ ] Every judgment carries a human-readable `reason`
- [ ] Duplicate findings from two agents collapse to one with raised confidence
- [ ] A finding already posted on an earlier commit of the same PR is not re-accepted
- [ ] A refuted finding is never accepted, regardless of the judge's output
- [ ] Sub-threshold findings are never accepted
- [ ] A judge failure results in `failed` and zero posted findings
- [ ] Accepted count for a typical PR is in the low single digits
- [ ] Rejection-code distribution is queryable per repository

## Risks

**A judge that rubber-stamps.** The failure mode that makes the phase decorative. Watch
the accept rate; if it exceeds roughly a third of candidates, the prompt is too
permissive.

**A judge that rejects everything.** Equally possible and harder to notice, because a
silent bot looks like a working bot on a clean codebase. Phase 16's recall measurement is
the only real defence; until then, review the rejection reasons by hand weekly.

**Per-finding judging.** Loses duplicate detection, contradiction detection, and volume
control. One call, whole review.

**Discarding duplicates instead of merging them.** Throws away independent agreement, the
best confidence signal there is.

**No cross-review dedupe.** The most visible failure to an actual user: a PR with four
pushes and the same comment four times. Build it in this phase, not as a Phase 9
afterthought.

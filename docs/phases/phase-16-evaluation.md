# Phase 16 — Evaluation

## Goal

Measure precision, recall, F1, false-positive rate, latency, and cost against a labelled
dataset of real pull requests. Turn every future change from a guess into a measurement.

## Depends on

Phase 15. **Unlocks** everything — this is where the project stops being a build and
starts being an optimisation loop.

## Why this is the phase that matters most

Before Phase 16, every question about PRGuard has an opinion for an answer. Is the new
security prompt better? Does `medium` effort hurt the bug agent? Is 0.85 the right
threshold? Would Sonnet be good enough for the evidence stage? Does the judge reject too
much?

With a dataset, each of those is a number. Without one, prompt tuning is superstition —
and worse, a regression is invisible: precision can drop from 0.8 to 0.5 across a month of
well-intentioned prompt edits with nothing to notice it.

**Especially optimise precision.** Developers stop trusting the bot if it raises bogus
comments, and trust does not come back.

## Scope

**In:** the dataset, its ground-truth format, the replay runner, metrics, reporting,
regression gating, sweeps.

**Out:** Automated prompt optimisation. Get the measurement right first; automated search
over a small dataset overfits fast.

## The dataset

```
evaluation/
    dataset/
        pr_001/
            meta.yaml           repo, PR number, base/head SHA, why it's included
            diff.patch          the frozen unified diff
            ground_truth.yaml   the labels
            context/            frozen tool responses (see determinism below)
        pr_002/
        ...
        pr_100/
```

100 historical PRs. Freeze everything — diffs move, repositories get rewritten, and a
dataset that fetches from GitHub at run time is not reproducible.

### Composition

A dataset of only bug-containing PRs measures recall and lies about precision. Aim for
roughly:

| Slice | Share | Purpose |
|---|---|---|
| PRs with a known real bug (found later, ideally by an incident or a revert) | 40% | Recall |
| Clean PRs that were merged and caused no incident | 30% | **Precision — the most important slice** |
| PRs with a known security issue | 10% | Security agent recall |
| PRs with a known performance issue | 10% | Performance agent recall |
| Adversarial: injection attempts, huge diffs, generated files, binaries, unparseable code | 10% | Guardrail regression |

The clean-PR slice is the one that gets skipped and the one that matters. Every comment
PRGuard posts on a clean PR is a false positive, and false positives are the failure mode
the product is designed against.

The best source of real bugs is your own history: `git log --grep='fix\|revert'`, then walk
back to the PR that introduced what was fixed. That gives a bug with a known location and
a known fix — the strongest possible label.

### Ground truth

```yaml
# evaluation/dataset/pr_042/ground_truth.yaml
pr: 42
repo: acme/payments
head_sha: e4f5a6b
verdict: has_bugs          # has_bugs | clean | adversarial

expected_findings:
  - file: payment/service.py
    line: 87
    line_tolerance: 3
    severity: HIGH
    category: BUG
    description: >
      process_payment is called before the idempotency key is persisted, so a
      retried request creates a second charge.
    must_find: true
    discovered_by: incident INC-2291
    fixed_in: PR #58

acceptable_findings:
  - file: payment/client.py
    category: RELIABILITY
    note: Missing timeout on the HTTP call — real, but was not the incident cause

forbidden_findings:
  - file: payment/service.py
    line: 12
    note: >
      The f-string here is a log message, not SQL. An earlier prompt version
      reported it as injection.
```

Three categories, each doing distinct work:

- **`expected_findings`** with `must_find: true` — recall. A miss is a false negative.
- **`acceptable_findings`** — real issues that are not the labelled bug. Reporting one is
  neither a hit nor a false positive. Without this category, a genuinely useful finding
  gets scored as an error and the metrics push the system toward silence.
- **`forbidden_findings`** — specific known false positives, usually harvested from real
  regressions. Reporting one is a hard failure. This list grows every time a bad comment
  is found in production, which makes the dataset a ratchet.

`line_tolerance` (default 3) handles the case where a finding is correct but anchored to
line 87 rather than 88 of the same expression.

### Determinism

The pipeline is non-deterministic (an LLM with adaptive thinking), so control everything
else:

- **Frozen diffs and metadata** — no GitHub calls at run time.
- **Frozen tool responses** under `context/` for the default run mode, so `read_file`,
  `search_code`, and symbol lookups return recorded results. This isolates prompt changes
  from repository drift.
- **A `--live-tools` mode** that runs the real tools against a checked-out SHA, used to
  validate the tool layer itself.
- **Pinned tool versions** — Semgrep, Ruff, and Bandit versions and rulesets are recorded
  in the dataset metadata (Phase 10 requires them pinned for exactly this reason).
- **N runs per PR**, default 3, reporting mean and standard deviation. A single run of a
  non-deterministic pipeline is an anecdote. High variance on a PR is itself a finding —
  it usually means the prompt is underspecified.

## The runner

```bash
python -m evaluation.runner \
    --dataset evaluation/dataset \
    --runs 3 \
    --output evaluation/results/2026-09-09-baseline.json

python -m evaluation.runner --compare \
    evaluation/results/2026-09-09-baseline.json \
    evaluation/results/2026-09-16-new-security-prompt.json
```

The runner drives the real pipeline — the same LangGraph graph, the same agents, the same
judge — with GitHub replaced by fixtures and posting replaced by capture. If it
reimplements any part of the pipeline, it is measuring something else.

Run in parallel with a concurrency cap, and record for each run: the config snapshot
(models, efforts, thresholds), prompt versions, and tool versions. A result without its
config is not comparable to anything.

## Metrics

### Matching

A produced finding matches an expected one when: same file, line within
`line_tolerance`, same category, and — judged by a matcher, not by string equality — the
same underlying issue.

The matcher is itself an LLM call (`claude-opus-5`, `effort: medium`) with a strict
structured output: *"do these two findings describe the same problem?"* Two safeguards
against measuring the matcher instead of the system: it never sees whether the finding was
accepted or rejected, and a random 10% sample of its judgments is hand-audited each time
the dataset changes.

### Definitions

Per PR, over **posted** findings only. What was rejected internally costs nothing and is
not scored — it is diagnostics.

```
TP  posted finding matching an expected finding
FP  posted finding matching nothing expected, and not in acceptable_findings
    (a forbidden_findings match is an FP and additionally a hard failure)
FN  expected finding with must_find: true that was not posted
TN  clean PR with zero posted findings
```

```
precision = TP / (TP + FP)
recall    = TP / (TP + FN)
F1        = 2PR / (P + R)
FPR       = FPs / total_clean_PRs          # per-PR, the number developers feel
```

Report the **per-PR false-positive rate** alongside the aggregate. "0.4 false positives
per clean PR" is a number a team can reason about; a precision figure of 0.83 is not.

### Reported

| Metric | Target | Notes |
|---|---|---|
| Precision | **> 0.85** | The primary metric |
| Recall | > 0.50 | Deliberately modest — half the bugs, reliably, beats all of them unreliably |
| F1 | > 0.63 | Derived; not a target in itself |
| FP per clean PR | **< 0.3** | Roughly one false positive per three clean PRs |
| Forbidden-finding hits | **0** | Hard gate |
| Latency p50 / p95 | < 3 min / < 8 min | |
| Cost per review p50 / p95 | < $0.60 / < $1.20 | |
| Cost per posted comment | tracked | The efficiency number |

Also break precision and recall down **per agent** and **per category**. An aggregate
precision of 0.85 can hide a performance agent at 0.55 dragging down two agents at 0.95 —
and the fix for that is specific, not general.

## Regression gating

Once a baseline exists, gate changes on it. Run the full dataset on any change to a
prompt, a model, an effort level, a threshold, or the judge:

| Condition | Result |
|---|---|
| Precision drops > 0.03 | **Block** |
| Any forbidden finding posted | **Block** |
| Recall drops > 0.05 | **Block** |
| Cost p50 rises > 20% | **Block** |
| Latency p95 rises > 30% | Warn |

A full 100-PR run at 3 runs each is ~300 reviews — real money and real time, so it is not
a per-commit gate. Run a 20-PR smoke subset on every PR to the PRGuard repository and the
full dataset nightly and before any release.

## Sweeps

The dataset's second job is answering the questions Phase 14 deferred:

| Sweep | Question |
|---|---|
| `effort` per stage | Does the evidence agent need `high`, or is `medium` equivalent? |
| `CONFIDENCE_THRESHOLD` | The precision/recall curve — where is the knee? |
| Model per stage | Is `claude-sonnet-5` adequate anywhere, and at what precision cost? |
| Agent ablation | What does each agent contribute? Does removing one hurt? |
| Evidence ablation | Which verification strategies actually change outcomes? |
| Evidence weights | Are the Phase 7 weights (`α`, `β`) right? |
| Context budget | Does more context improve precision, or just cost more? |

Two of these are especially worth running early. **The threshold sweep** produces the
precision/recall curve that turns "0.85 feels right" into a defended choice. **The
evidence ablation** tells you whether the most expensive part of the pipeline is earning
its cost — and if some verification strategy never changes an outcome, that is a
strategy to delete.

Guard against overfitting: hold out 20 PRs, never sweep against them, and validate the
final configuration there. A configuration tuned to 100 PRs and validated on none is
tuned to noise.

## Maintaining the dataset

The dataset decays. Keep it alive:

- **Every production false positive becomes a `forbidden_findings` entry.** This is the
  ratchet, and it is the single highest-value habit in the project.
- **Every missed bug found by a human reviewer** becomes an `expected_findings` entry.
- **Add ~10 PRs a quarter**, weighted toward recently-changed parts of the codebase.
- **Re-audit labels annually.** A "clean" PR that caused an incident six months later was
  mislabelled, and it is now a bug case.
- **Version the dataset** and record the version in every result file. Comparing results
  across dataset versions is invalid unless you know they differ.

## Reporting

```
PRGuard Evaluation · dataset v3 · 2026-09-09 · 100 PRs × 3 runs

Precision          0.87  ± 0.02      target > 0.85   ✓
Recall             0.54  ± 0.04      target > 0.50   ✓
F1                 0.67
FP per clean PR    0.23              target < 0.30   ✓
Forbidden hits     0                 target 0        ✓

Latency  p50 1m 52s   p95 6m 20s
Cost     p50 $0.48    p95 $1.10     per comment $0.19

By agent          precision   recall   posted
  bug                  0.91     0.61       74
  security             0.89     0.58       31
  performance          0.71     0.34       19   ◀── worst precision

Regressions vs 2026-09-02: none
Improvements: security precision 0.84 → 0.89 (new dataflow evidence strategy)
```

Commit reports to the repository. The history of that file is the project's actual
progress record — more informative than the commit log, because it says whether the
changes worked.

## Exit criteria

- [ ] 100 PRs labelled, with the composition mix above respected
- [ ] The clean-PR slice is at least 30% of the dataset
- [ ] All diffs, metadata, and tool responses frozen; no network at run time
- [ ] Runner drives the real pipeline, not a reimplementation
- [ ] Config, prompt versions, and tool versions recorded in every result
- [ ] N-run averaging with variance reported
- [ ] Matcher validated by a hand-audited 10% sample
- [ ] Precision, recall, F1, per-PR FPR, latency, and cost all reported
- [ ] Per-agent and per-category breakdowns
- [ ] Baseline committed
- [ ] Regression gate wired into CI: 20-PR smoke per PR, full dataset nightly
- [ ] Threshold sweep produces a precision/recall curve
- [ ] Evidence ablation quantifies each strategy's contribution
- [ ] A 20-PR holdout set exists and has not been swept against

## Risks

**A dataset of only buggy PRs.** Measures recall, reports nothing about precision, and
optimises the system toward finding something everywhere.

**No `acceptable_findings` category.** Punishes genuinely useful findings as errors and
drives the system toward silence.

**Single-run evaluation.** A non-deterministic pipeline measured once produces numbers
that move on their own.

**Overfitting to 100 PRs.** Hold out 20. A configuration that is excellent on the sweep
set and mediocre in production is the expected outcome without a holdout.

**Measuring the matcher.** A weak matcher makes every number noise. Audit it, and keep it
blind to the accept/reject decision.

**Letting the dataset rot.** An unmaintained dataset gives confident numbers about a
system that has moved on. The forbidden-findings ratchet is what keeps it honest.

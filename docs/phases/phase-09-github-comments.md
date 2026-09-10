# Phase 9 — GitHub Comments

## Goal

Post accepted findings as inline review comments on the pull request. This closes the
loop and makes PRGuard a product.

## Depends on

Phase 8. **Unlocks** Phases 10–16 (all of which improve a working system).

## Scope

**In:** output guardrails, comment rendering, the GitHub review API, the review summary,
update behaviour on re-review, `DRY_RUN`.

**Out:** Resolving or deleting stale comments from previous commits — noted as a known
limitation below.

## Flow

```
Judge
  │
  ▼
Output Guardrail          ← deterministic, mandatory, last line of defence
  │
  ▼
GitHub API                POST /repos/{o}/{r}/pulls/{n}/reviews
  │
  ▼
PR Review                 one review, N inline comments, one summary
```

## Output guardrails

The judge decided *whether* to post. The guardrails check *that posting is safe*. They
are deterministic, they run on every finding, and nothing reaches the API without passing
them.

```
Does the file exist at head SHA?
        ↓
Does the line exist in that file?
        ↓
Is the line inside the diff? (present in diff_line_map)
        ↓
Is this finding a duplicate of one already posted on this PR?
        ↓
Is confidence > CONFIDENCE_THRESHOLD?
        ↓
Is severity >= MIN_SEVERITY_TO_POST?
        ↓
Does the body contain no secrets? (Phase 12)
        ↓
      POST
```

| Check | Failure |
|---|---|
| File exists at `head_sha` | Drop, `unknown_file` |
| Line within the file's line count | Drop, `line_out_of_range` |
| Line present in `diff_line_map` | Drop, `outside_diff` |
| Not already posted on this PR | Drop, `duplicate` |
| `confidence > CONFIDENCE_THRESHOLD` | Drop, `below_threshold` |
| `severity >= MIN_SEVERITY_TO_POST` | Drop, `below_threshold` |
| Body free of secret patterns | Drop, `secret_in_output` (Phase 12) |
| Body within GitHub's 65536-char limit | Truncate with a marker |

Yes, several of these duplicate Phase 8 checks. That is intentional — this is the last
gate before an irreversible, developer-visible action, and it is deterministic where the
judge is not. Redundancy at a trust boundary is not waste.

Every drop here is logged at WARNING with the finding ID. A guardrail firing means an
upstream phase produced something it should not have.

## Anchoring comments

The single most important mechanical detail in the phase. GitHub's review API anchors an
inline comment to a **position in the diff**, not a line number in the file.

Use the mapping computed in Phase 3:

```python
position = review_file.diff_line_map[str(finding.line)]
```

If the line is absent from the map, the line is not in the diff — drop the finding. Do
not fall back to a nearby position, do not guess, do not post it as a top-level comment
instead. A comment on the wrong line is worse than no comment: it makes the bot look
broken and the finding unverifiable.

The newer `line` + `side` + `start_line` parameters are also available and read more
naturally than `position`. Either works; pick one and use it consistently. `position` has
the advantage of being unambiguous about which diff it refers to, which matters when the
PR has moved on since the review started.

## Comment body

```markdown
**🔴 HIGH · BUG** · confidence 0.96

Duplicate payment possible on retry

`process_payment` is called before the idempotency key is persisted, so a retried
request creates a second charge. There is no unique constraint on
`payments.idempotency_key`, and the retry path at `client.py:44` reaches this line.

<details><summary>Evidence</summary>

- `ast`: `process_payment` call precedes the `session.commit()` at line 91
- `search`: no unique index on `payments.idempotency_key` in `db/migrations/`
- `dataflow`: reached from `client.py:44` retry handler

</details>

<sub>PRGuard · [review 7f3a2b](…) · reply with `@prguard ignore` to suppress</sub>
```

Rules for the body:

- **Severity and category up front**, with a colour marker. Scannable in a PR timeline.
- **The title as a single line**, then the description. No heading hierarchy — GitHub
  comments are small.
- **Evidence in a collapsed `<details>` block.** Present for anyone who wants to check,
  invisible to anyone who does not. This is what makes a finding defensible instead of an
  assertion.
- **Confidence shown.** Developers calibrate on it quickly, and hiding it reads as
  overconfidence.
- **A footer with the review ID.** Every comment is traceable to a review, its findings,
  its evidence, and its cost.
- **No suggested-changes blocks.** Auto-fix is a non-goal
  ([00-overview.md](../00-overview.md)); a wrong suggestion that someone clicks is a much
  worse outcome than a wrong comment they read.

Escape anything derived from repository content. A file path or code snippet containing
backticks or markdown will otherwise mangle the comment — and code from the PR is
attacker-controlled (Phase 12).

## Posting

One review, not N comments. `POST /repos/{owner}/{repo}/pulls/{n}/reviews` with all
comments in a single request:

```python
{
    "commit_id": head_sha,
    "event": "COMMENT",
    "body": summary_markdown,
    "comments": [{"path": f.file_path, "position": pos, "body": rendered} for f, pos in anchored],
}
```

Why one call:

- **Atomic.** Either the review posts or it does not. Fifteen individual comment calls
  can fail halfway and leave a partial review that cannot be cleanly retried.
- **One notification.** Fifteen calls send fifteen emails to everyone watching the PR.
- **Rate limits.** One request instead of fifteen.

`event: "COMMENT"` always. Never `REQUEST_CHANGES` — PRGuard advises, it does not block
merges. Blocking on a bot's judgment is how a code review tool gets removed.

**Always pass `commit_id: head_sha`.** Without it GitHub anchors to the latest commit,
which may not be the one that was reviewed.

If the head SHA has moved since the review started, do not post — the diff positions no
longer refer to the same diff. Mark `skipped` with reason `superseded`; the newer commit
has its own review in flight.

### Zero findings

Post nothing at all. Not a "no issues found" comment.

A bot that comments on every PR trains people to ignore its comments. Silence is the
correct output for a clean PR, and the review record in the database is where "we did
look" is recorded. The exception is a partial review, where the *limitation* is worth
saying — see below.

## The summary body

Short, factual, and only when there is something to say:

```markdown
Reviewed 7 files (+210 −80) in 2m 14s · 3 findings

⚠️ Partial review: the diff exceeds the 5000-line budget. Reviewed 12 of 47 files;
`vendor/`, `poetry.lock`, and 33 other files were not analysed.
```

The partial-review warning is not optional. A review that presents itself as complete
when it examined a quarter of the diff is a correctness bug
([Phase 3](phase-03-pr-analyzer.md)).

## Re-review on new commits

Each `head_sha` gets its own review, and each posts its own GitHub review. Cross-review
deduplication in Phase 8 prevents re-posting a finding that is still open on an untouched
line.

**Known limitation:** comments from a previous commit are not resolved or deleted when
the code they refer to is fixed. GitHub marks them outdated automatically once the diff
moves, which is adequate but not tidy. Proper resolution needs the GraphQL
`resolveReviewThread` mutation and a mapping from finding to thread ID — worth doing, and
deliberately deferred rather than rushed into this phase.

## `DRY_RUN`

`DRY_RUN=true` runs the entire pipeline, renders every comment, logs it at INFO, and posts
nothing. This is how you evaluate a prompt change, a threshold change, or a new repository
without spending anyone's attention.

Run every new installation in dry-run for its first week. Read every comment it would
have posted. That exercise is worth more than any metric available before Phase 16.

## Exit criteria

- [ ] Accepted findings appear as inline comments on the correct lines of a real PR
- [ ] Every comment renders severity, category, confidence, description, and evidence
- [ ] A clean PR receives no comment of any kind
- [ ] A partial review states its limitation in the summary
- [ ] All comments post in a single API request with `commit_id` set
- [ ] `event` is always `COMMENT`; merges are never blocked
- [ ] Pushing a new commit does not re-post an unchanged finding
- [ ] A moved head SHA aborts posting
- [ ] `DRY_RUN=true` posts nothing and logs everything
- [ ] Manually verified on a real PR (multi-hunk file, and a hunk at line 1) that anchoring is exact
- [ ] `findings.posted_at` and `github_comment_id` are recorded

## Risks

**Off-by-one anchoring.** The defining bug of this phase. Verify on a real PR with
multiple hunks in one file, and a hunk starting at line 1.

**Posting fifteen separate comments.** Fifteen notifications and a non-atomic review.
One request.

**`REQUEST_CHANGES`.** Turns advice into a merge blocker and turns one false positive
into a blocked release.

**A "no issues found" comment.** Every PR gets a notification, and within two weeks
nobody reads any of them.

**Unescaped repository content in the body.** Broken markdown at best; at worst, code
from the PR rendering as instructions in a comment that a human reads as PRGuard's own
voice.

**Skipping the manual verification.** Lint and type checks pass happily while every
comment lands one line off. Post to a real PR — with multiple hunks in one file, and a
hunk starting at line 1 — and read where the comments actually landed. There is no
substitute for this one.

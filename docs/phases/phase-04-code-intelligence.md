# Phase 4 — Code Intelligence Layer

## Goal

Five tools that let an agent explore a repository on demand, behind a single gateway that
dispatches, permission-checks, and audits every call. Still no LLM — the tools are built
and tested as plain functions first.

## Depends on

Phase 3. **Unlocks** Phase 5.

## Why this comes before the reviewer

A diff alone is not reviewable. `process_payment(order)` changed — is that safe? The
answer is in the function's definition, in its three call sites, and in whether a test
covers the new branch. None of that is in the diff.

The naive alternative is to send the repository. At 1M context that is even technically
possible, and it is still wrong: it costs two orders of magnitude more, it buries the
diff in noise, and precision drops because the model has four hundred files' worth of
plausible things to comment on. Tools invert this — the agent asks for what it needs.

## Scope

**In:** repository checkout, the five tools, the gateway, token budgeting for tool
results, the audit log.

**Out:** Executing anything from the repository (Phase 11). The tools in this phase read
and parse; they never run.

## Architecture

```
                 Agent
                   │  tool call (name, args)
                   ▼
             Tool Gateway
             ├── permission check
             ├── argument validation
             ├── rate limit (per review)
             ├── result size cap
             └── audit → tool_calls
                   │
       ┌───────────┼───────────┬──────────────┐
       ▼           ▼           ▼              ▼
  read_file   search_code   Tree-sitter    get_tests
              (ripgrep)     (AST index)
                   │
                   ▼
          Repository working tree
          (shallow clone at head_sha, read-only)
```

## Repository checkout

Tools need a working tree, not API calls — `search_code` over the REST API is impossible
and `read_file` would be one request per file.

```bash
git clone --depth 50 --filter=blob:none --no-checkout <url> <workdir>
git -C <workdir> checkout <head_sha>
```

- `--depth 50` gives enough history for blame-adjacent questions without a full clone.
- Authenticate with the installation token in the URL, then **immediately** rewrite the
  remote to strip it — otherwise the token sits in `.git/config` inside a tree that later
  phases mount into a sandbox.
- Working directory under a per-review temp path, deleted in a `finally` block. Always,
  including on failure and on soft-timeout. A leaked clone of a private repository is an
  incident.
- Mark the tree read-only (`chmod -R a-w`) for this phase. Phase 11 needs writes; it gets
  a separate copy.
- Cache clones per repository, keyed by repo ID, updated with `git fetch` rather than
  re-cloned. Cache invalidation is by disk pressure and age, not correctness — a checkout
  is always to an explicit SHA.

## The tools

Each tool is a pure function over `(repo_root, args) → structured result`. Keep a small
sample repository under `samples/repo/` with known symbols, call sites, and tests — it is
what you point the tools at while building them.

### 1. `read_file(path, start_line=None, end_line=None)`

Read a file, or a line range, from the checked-out tree.

- Path is resolved and verified to be inside `repo_root`. Reject `..`, absolute paths,
  and symlinks that escape. This is the highest-risk tool for path traversal because its
  argument is model-supplied — use `Path.resolve()` and `is_relative_to(root)`, never
  string prefix matching.
- Returns content with line numbers attached, so the agent can cite a line and the
  citation can be validated later.
- Caps at `MAX_FILE_BYTES`; when exceeded, return the head, the tail, and an explicit
  `truncated: true` with the omitted range. Never silently truncate.
- Refuses binaries — returns a typed "binary file, N bytes" result rather than garbage.

### 2. `search_code(pattern, path_glob=None, max_results=50)`

Ripgrep. Chosen over a Python regex walk because it is an order of magnitude faster on a
large repository and it respects `.gitignore` for free.

```bash
rg --json --line-number --max-count 3 --glob '<glob>' -e '<pattern>' <root>
```

- `--json` gives structured output — parse it, do not regex ripgrep's human format.
- `--max-count 3` per file keeps one hot pattern from consuming the whole result budget.
- Return path, line number, the matched line, and two lines of context either side.
- Validate the pattern: reject patterns that are catastrophically backtracking-prone, and
  set a hard subprocess timeout. A model can and will send `(a+)+b`.
- Bound total results at `max_results` and report the true total separately, so the agent
  knows whether it saw everything.

### 3. `get_symbol(name, kind=None)`

Tree-sitter. Find where a function, class, method, or import is *defined*.

- Grammar: Python in this phase. The parser registry is keyed by `language` from Phase 3,
  so adding JS/TS later is a registration, not a refactor.
- Returns for each match: path, kind (`function` / `class` / `method` / `import`), the
  qualified name, the start and end lines, the signature, and the docstring if present.
- Extract the **full body** only on request (`include_body=True`) — most questions are
  answered by the signature, and bodies are expensive.
- Build the AST index lazily per file, cache per `(path, mtime)`. Indexing an entire
  large repository up front costs seconds the review does not have; the agent only ever
  asks about a handful of files.
- Tree-sitter is error-tolerant by design: a file with a syntax error still parses
  partially. Return what parsed and flag it, rather than failing the tool call.

### 4. `find_references(name, kind=None)`

Where is this symbol *used*? The tool that most often distinguishes a real finding from a
hypothetical one — "this can be called with `None`" is only true if some caller does.

- Two-pass: `search_code` for the identifier to get candidate files cheaply, then
  Tree-sitter over just those files to filter to genuine references — call expressions,
  attribute access, imports — and drop comments, strings, and unrelated same-name locals.
- Returns path, line, the enclosing function or class, and the reference kind
  (`call` / `import` / `attribute` / `assignment`).
- Explicitly not a full resolver. It does not do type inference or handle dynamic
  dispatch, and the result carries `confidence: "heuristic"` so the evidence agent
  (Phase 7) weights it accordingly. Claiming precision it does not have is worse than
  admitting the limit.

### 5. `get_tests(symbol=None, path=None)`

Find tests related to a changed function or file. Used both for context ("is this covered")
and, from Phase 11, to decide which tests to actually run.

Heuristics, in order, each contributing results:

1. Naming convention — `foo()` → `test_foo`, `TestFoo`, `test_foo_*`.
2. Path convention — `src/payment/service.py` → `tests/payment/test_service.py`,
   `tests/test_service.py`, `src/payment/tests/`.
3. Import-based — files under a test path that import the changed module. This is the
   strongest signal and the one that catches integration tests.
4. Reference-based — `find_references` restricted to `category = test`.

Return path, test function name, line, and which heuristic matched. Deduplicate.

## The gateway

The single dispatch point, and the reason the tools are worth building as a layer rather
than as helper functions.

```python
class ToolGateway:
    async def call(self, agent: str, tool: str, args: dict) -> ToolResult: ...
```

Responsibilities:

- **Registry** — name → callable + Pydantic argument schema. Invalid arguments return a
  typed error result the agent can recover from, not an exception that kills the review.
- **Permissions** — per-agent allowlists. The performance agent has no reason to read
  `.env`; the security agent has no reason to run tests. Denials are recorded with
  `allowed = false`, which makes over-broad access visible before Phase 11 makes it
  dangerous.
- **Rate limiting** — per review: `MAX_TOOL_CALLS_PER_REVIEW`, and a per-tool cap.
  Prevents a confused agent from issuing four hundred `search_code` calls.
- **Result budgeting** — every result is token-counted before it reaches the model, and
  the running total is enforced against `MAX_CONTEXT_TOKENS`. Count with the API's
  `count_tokens`, not a character heuristic — see
  [04-llm-strategy.md](../04-llm-strategy.md).
- **Audit** — one `tool_calls` row per call: agent, tool, args, allowed, result bytes,
  duration. This is how you answer "why did it say that" in Phase 15.
- **Errors as values** — a failing tool returns `ToolResult(ok=False, error=...)`. The
  agent sees the failure and adapts. Nothing about a missing file should abort a review.

## Context builder

`agent/context/builder.py` uses the tools to assemble the bundle the reviewers share:

For each reviewable changed file, in budget order:
1. The diff hunks with surrounding context lines.
2. Definitions of symbols the hunks touch (`get_symbol`, signatures only unless small).
3. References to changed public symbols (`find_references`, capped per symbol).
4. Related tests (`get_tests`, names and paths, bodies only for directly-named tests).

Render deterministically — sorted keys, stable ordering, no timestamps — because this
bundle is a prompt-cache breakpoint shared by all three reviewer agents. A
non-deterministic renderer silently costs ~70% more per review.

Trim from the bottom of the priority list when over budget, and record what was trimmed.

## Exit criteria

- [ ] Shallow clone at a specific SHA works, and the temp tree is deleted on every path
- [ ] No credential remains in `.git/config` after checkout
- [ ] All five tools return correct structured results against the sample repo
- [ ] Every path-traversal attempt is rejected
- [ ] Gateway enforces permissions, rate limits, and the token budget
- [ ] One `tool_calls` row per invocation, including denials
- [ ] Context bundle renders deterministically and stays under `MAX_CONTEXT_TOKENS`

## Risks

**Path traversal.** The tool arguments come from a model reading attacker-influenced
code. Resolve and verify containment; do not trust `startswith`. Try `../../etc/passwd`,
an absolute path, and a symlink pointing outside the tree against each tool once, by
hand — this is the one place in the project worth a deliberate adversarial pass.

**Building a full AST index eagerly.** Tempting and slow. Index lazily, cache by mtime.

**Over-claiming from `find_references`.** A heuristic reference list presented as
authoritative leads the evidence agent to confirm findings it should have doubted. Label
it.

**Leaving the clone behind.** Wrap in `try/finally`, and also have the Phase 13 reaper
clean orphaned working directories — a SIGKILL skips `finally`.

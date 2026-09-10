# Phase 11 — Docker Sandbox

## Goal

Execute code from the pull request — tests, builds, analysis — inside a container with
hard resource, filesystem, and network limits. A failing test becomes the strongest
evidence PRGuard can produce.

## Depends on

Phase 10. **Unlocks** Phase 12.

## The rule

> **Never run arbitrary PR code directly inside the API or worker process.**

A pull request is attacker-controlled input. Running its test suite means executing code
written by whoever opened the PR — and on a public repository, that is anyone. Without a
sandbox, `conftest.py` in a fork's PR is remote code execution on infrastructure holding
a GitHub App private key that can read every repository the app is installed on.

This is why the phase comes after a working product rather than before it: nothing in
Phases 1–10 executes repository code, so there is no window where PRGuard runs untrusted
tests unsandboxed "just for now".

## Scope

**In:** sandbox image, container lifecycle driver, resource limits, the `run_tests` tool,
permission gating in the gateway, moving Semgrep and Bandit into the sandbox.

**Out:** Arbitrary agent-authored shell commands. The sandbox runs a fixed set of
allowlisted commands with validated arguments — the model chooses *which* test to run,
never *what command* to run.

## Architecture

```
Agent
  │  run_tests(path="tests/test_payment.py::test_retry")
  ▼
Tool Gateway
  │
  ▼
Permission Check      ← is this agent allowed to execute? is the target a test file?
  │
  ▼
Docker Sandbox
  ├── pytest
  ├── build / import check
  └── static analysis (semgrep, bandit — moved here from Phase 10)
```

## Container configuration

Every limit in the table is enforced, and the settings validator refuses to boot in
production if any of them is weakened
([05-configuration.md](../05-configuration.md)).

| Control | Setting | Why |
|---|---|---|
| CPU | `--cpus 2.0` | A crypto-miner in a test fixture gets two cores, not the host |
| Memory | `--memory 2g --memory-swap 2g` | Equal values disable swap; an OOM kills the container, not the host |
| PIDs | `--pids-limit 256` | Fork-bomb containment |
| Wall clock | 300 s, killed by the driver | Docker has no built-in timeout |
| User | `--user 10001:10001` | Non-root, always. A root process in a container is one escape from root on the host |
| Root filesystem | `--read-only` | Writable `tmpfs` at `/tmp` (256 MB, `noexec`) and the workspace mount only |
| Network | `--network none` | The default. No exfiltration, no dependency download, no callback |
| Capabilities | `--cap-drop ALL` | Nothing needs `CAP_NET_RAW` to run pytest |
| Privilege escalation | `--security-opt no-new-privileges` | Blocks setuid escalation |
| Seccomp | Default Docker profile, minimum | Consider a tightened profile later |
| Devices | No device mounts, no Docker socket | **Never** mount `/var/run/docker.sock` |
| Persistence | `--rm`, fresh container per job | No state survives between reviews |
| Ulimits | `nofile=1024`, `fsize` capped | Descriptor and disk-fill containment |

### The network question

`SANDBOX_NETWORK_MODE=none` is the default and the correct answer for most repositories.
The cost is that tests requiring dependency installation will not run.

Dependencies are therefore installed at **image build time**, not at review time. Build a
per-repository image from the repository's lockfile, cache it, and rebuild when the
lockfile changes. This is more work than `pip install` inside the container, and it is the
difference between a sandbox and a machine with internet access running attacker code.

If a repository genuinely needs egress, `restricted` mode uses a dedicated bridge network
with an egress proxy allowlisting only the package index. It is opt-in per repository,
documented as a reduced-safety mode, and `bridge`/`host` are hard-rejected in production.

## Sandbox image

`prguard/sandbox:latest`, built from a slim Python base:

- Python 3.12, pytest, and the analysis tools (Semgrep, Bandit, Ruff) pinned.
- No compilers, no `curl`, no `wget`, no `git`, no shell utilities beyond what pytest
  needs. Every binary present is a capability granted to attacker code.
- Non-root user `10001` created at build time; `/workspace` owned by it.
- No secrets, no credentials, no environment variables from the host. The container's
  environment is constructed explicitly, and the GitHub token is never in it.

Rebuild and rescan the image on a schedule — a sandbox image with a known-vulnerable
dependency is a sandbox with a known escape.

## Workspace mounting

The repository is mounted **read-only** at `/workspace/repo`, with a writable `tmpfs`
overlay for pytest's cache and any test-created temp files:

```
--mount type=bind,source=<clone>,target=/workspace/repo,readonly
--mount type=tmpfs,target=/tmp,tmpfs-size=256m,tmpfs-mode=1777,noexec
--mount type=tmpfs,target=/workspace/.pytest_cache,tmpfs-size=64m
```

Read-only is not optional: a writable mount lets a test modify the working tree that
later evidence checks read, which is a straightforward way to fabricate evidence.

Before mounting, confirm the clone contains no credentials — Phase 4 strips the token
from `.git/config`, and this phase asserts it.

## The `run_tests` tool

```python
def run_tests(
    path: str,                      # file, or file::test_name
    timeout_seconds: int = 300,
) -> TestResult
```

Gateway checks before the container starts:

1. Permission: is the calling agent allowed to execute? Only the **evidence agent** is.
   Reviewer agents never execute code.
2. Target validation: `path` resolves inside `/workspace/repo` and the file's Phase 3
   `category` is `test`. No running arbitrary modules.
3. Argument validation: the test-selection string matches a strict pattern
   (`[\w/.-]+(::[\w\[\]-]+)?`). No shell metacharacters, no flags — the command is
   constructed from a template, never interpolated from model output.
4. Rate limit: `MAX_SANDBOX_RUNS_PER_REVIEW`, default 3. Test execution is the most
   expensive evidence available and it is not always worth it.

The command is fixed:

```bash
python -m pytest <validated-path> -x -q --no-header \
  --timeout=60 --json-report --json-report-file=/tmp/report.json
```

`TestResult` carries: exit code, passed/failed/errored counts, per-failure test name,
assertion message, and traceback tail, plus a `timed_out` flag and truncated stdout/stderr
(64 KB caps).

## Interpreting the result

Test evidence is powerful and easy to misread:

| Outcome | Evidence |
|---|---|
| A test fails, and the failure is about the finding's code path | `supports`, weight **1.00** — the strongest evidence in the system |
| A test fails for an unrelated reason (import error, fixture, environment) | `error` — contributes nothing |
| Relevant tests pass | `refutes`, weight **0.50** |
| Test run timed out | `error` |
| No relevant tests exist | `inconclusive` |

The `refutes` weight is deliberately low. Passing tests mean the existing tests do not
cover the bug — which is often *why* the bug exists. Treating a green suite as a strong
refutation would suppress precisely the findings worth reporting.

Distinguishing "failed because of the finding" from "failed for unrelated reasons"
requires judgment, so the evidence agent reads the failure output. Establish a baseline
first: run the same tests at the **base SHA** and compare. A test failing at both SHAs is
pre-existing noise, not evidence about this PR. This doubles the cost of test evidence,
which is why the per-review run cap is low.

## Moving Semgrep and Bandit into the sandbox

Phase 10 ran them in the worker. They only read code, so the risk is lower than executing
tests — but they are large dependencies parsing attacker-controlled input, and a parser
bug in either is a worker compromise. Move them in this phase. The interface does not
change; only the execution location does.

## Failure handling

| Failure | Behaviour |
|---|---|
| Docker daemon unreachable | All sandbox evidence becomes `error`; review completes degraded |
| Image missing | Pull once, then `error`; never build at review time |
| Container OOM-killed | `error` with the OOM flag recorded; not a test failure |
| Timeout | Kill (`SIGKILL` after `SIGTERM`), record `timed_out` |
| Container will not stop | Force-remove; alert. A container ignoring SIGKILL is an incident |
| Orphaned containers | Phase 13's reaper removes containers labelled with a stale review ID |

Label every container `prguard.review_id=<uuid>` so orphans are attributable and
sweepable.

## Verifying the sandbox

The one place in this project worth a deliberate, repeatable adversarial pass. A sandbox
regression is completely silent — nothing fails, escape just becomes possible — so this
does not get to rely on noticing.

Keep a sample repository under `samples/adversarial/` whose `conftest.py` attempts each
of:

| Attempt | Expected |
|---|---|
| Read `/etc/passwd` | Permitted inside the container, but reveals only container state |
| Write to the mounted repo | Fails — read-only mount |
| Open an outbound socket | Fails — `--network none` |
| Fork 1000 processes | Fails — `--pids-limit` |
| Allocate 4 GB | OOM-killed, recorded as `error`, host unaffected |
| Sleep past the timeout | Killed at `SANDBOX_TIMEOUT_SECONDS`, `timed_out` recorded |
| Read host environment | Sees only the explicitly constructed environment; no GitHub token |
| Write to `/` | Fails — read-only root filesystem |

Run it by hand after any change to the sandbox driver, the image, or the compose file,
and confirm each row. Also confirm with `docker inspect` that the running container
actually carries the limits from the configuration table — a limit that silently failed
to apply looks identical to one that is working.

## Exit criteria

- [ ] Sandbox image builds and runs as non-root with a read-only root filesystem
- [ ] Every limit in the configuration table is enforced, confirmed via `docker inspect`
- [ ] `--network none` verified by an attempted outbound connection failing
- [ ] The Docker socket is never mounted
- [ ] The adversarial sample repo has been run once — every escape attempt contained and logged
- [ ] Only the evidence agent can call `run_tests`; denials recorded
- [ ] Command arguments are template-constructed, never interpolated
- [ ] A failing test produces weight-1.00 `supports` evidence
- [ ] A pre-existing failure is identified via the base-SHA baseline and discounted
- [ ] Timeouts kill the container and are recorded
- [ ] Semgrep and Bandit run inside the sandbox
- [ ] No container survives its review; orphans are labelled and sweepable
- [ ] Production boot fails if the network mode or user is weakened

## Risks

**Mounting the Docker socket.** Container escape to host root in one step. There is no
legitimate reason for it here.

**Running as root inside the container.** Turns a kernel or runtime bug into host root
instead of an unprivileged user.

**Installing dependencies at review time.** Requires network access, which defeats the
sandbox's main control. Build per-repository images.

**A writable repository mount.** Lets test code alter the evidence other checks read.

**Interpolating model output into a shell command.** The classic. Templates and strict
argument validation, never string formatting.

**Trusting a green test suite.** Low weight, deliberately. The absence of a test for a bug
is not the absence of the bug.

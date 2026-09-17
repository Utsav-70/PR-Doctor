"""Shallow checkout of a repository at one commit.

Tools need a working tree: `search_code` over the REST API is impossible, and
`read_file` would be one HTTP request per file.

Fetch by SHA rather than clone-then-checkout. `git clone --depth 50` of the default
branch frequently does *not* contain a PR head — the branch may have diverged well
beyond fifty commits, or the PR may target a non-default base. Fetching the exact
object is both correct and smaller.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from github.app_auth import get_installation_token
from settings import get_settings

logger = logging.getLogger(__name__)

CLONE_DEPTH = 50
GIT_TIMEOUT_SECONDS = 120.0


class CheckoutError(RuntimeError):
    pass


async def _git(*args: str, cwd: Path | None = None, redact: str = "") -> str:
    """Run git, returning stdout. Never lets a token reach a log or an exception."""
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # Refuse any interactive credential prompt rather than hanging the worker.
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"},
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=GIT_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        raise CheckoutError(f"git {args[0]} timed out after {GIT_TIMEOUT_SECONDS}s") from None

    if process.returncode != 0:
        message = err.decode("utf-8", "replace").strip()
        if redact:
            message = message.replace(redact, "***")
        raise CheckoutError(f"git {args[0]} failed: {message[:500]}")
    return out.decode("utf-8", "replace")


def _make_writable(path: Path) -> None:
    """Restore write permission so the tree can be deleted.

    `chmod -R a-w` clears the write bit on directories too, and a directory without it
    cannot have its entries unlinked — so cleanup has to undo it first.
    """
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            target = Path(root) / name
            with suppress(OSError):
                target.chmod(target.stat().st_mode | stat.S_IWUSR)
    with suppress(OSError):
        path.chmod(path.stat().st_mode | stat.S_IWUSR)


def _make_read_only(path: Path) -> None:
    """Strip write permission across the tree.

    This phase only reads. Phase 11 needs writes and gets its own copy, so anything
    mutating the tree here is a bug worth surfacing immediately.
    """
    for root, dirs, files in os.walk(path):
        if ".git" in dirs:
            dirs.remove(".git")  # git needs its own metadata writable
        for name in files:
            target = Path(root) / name
            with suppress(OSError):
                target.chmod(target.stat().st_mode & ~0o222)


@asynccontextmanager
async def checkout(
    *,
    installation_id: int,
    owner: str,
    repo: str,
    sha: str,
) -> AsyncIterator[Path]:
    """Yield a read-only tree at `sha`, and delete it on every exit path.

    A leaked clone of a private repository is an incident, so the cleanup is in a
    `finally` with no conditions on it. A SIGKILL still skips it — Phase 13's reaper
    covers that case and this one cannot.
    """
    settings = get_settings()
    token = await get_installation_token(installation_id)
    workdir = Path(tempfile.mkdtemp(prefix=f"prguard-{repo}-{sha[:7]}-"))
    host = settings.GITHUB_API_URL.replace("https://api.", "").rstrip("/") or "github.com"
    clean_url = f"https://{host}/{owner}/{repo}.git"
    auth_url = f"https://x-access-token:{token}@{host}/{owner}/{repo}.git"

    try:
        await _git("init", "--quiet", str(workdir))
        await _git("remote", "add", "origin", auth_url, cwd=workdir, redact=token)
        await _git(
            "fetch",
            "--quiet",
            "--depth",
            str(CLONE_DEPTH),
            "origin",
            sha,
            cwd=workdir,
            redact=token,
        )

        # Strip the credential before anything else touches the tree. It is written
        # into .git/config by `remote add`, and later phases mount this directory into
        # a sandbox — a token left here outlives the review.
        await _git("remote", "set-url", "origin", clean_url, cwd=workdir, redact=token)
        _assert_no_credential(workdir, token)

        await _git("checkout", "--quiet", "--detach", "FETCH_HEAD", cwd=workdir)
        _make_read_only(workdir)

        logger.info(
            "checkout ready",
            extra={"repo": f"{owner}/{repo}", "sha": sha[:7], "path": str(workdir)},
        )
        yield workdir
    finally:
        _make_writable(workdir)
        shutil.rmtree(workdir, ignore_errors=True)
        logger.debug("checkout removed", extra={"path": str(workdir)})


def _assert_no_credential(workdir: Path, token: str) -> None:
    """Fail loudly if the token survived anywhere in .git.

    Exit criterion for this phase, and cheap enough to check every time rather than
    trusting that `set-url` did what it says.
    """
    git_dir = workdir / ".git"
    for path in git_dir.rglob("*"):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if token in content or "x-access-token:" in content:
            raise CheckoutError(f"credential survived in {path.relative_to(workdir)}")

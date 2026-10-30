"""Prompt composition.

Three agent prompts that share ~80% of their text. Held as one shared skeleton with two
substitution points rather than three whole files, because the documented failure mode is
the three drifting apart: a scope rule fixed in one prompt and silently missing from the
other two produces findings that disagree about what is in scope, and nothing surfaces it.

    _shared.md      scope, citation, coverage-not-filtering, untrusted content
    <agent>.md      the role line and the domain taxonomy, and nothing else

Each agent file carries exactly two markers, so adding an agent means writing its domain
section and nothing else.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent
ROLE_MARKER = "<!-- role -->"
DOMAIN_MARKER = "<!-- domain -->"

# Agent name -> prompt file. `reviewer` is the Phase 5 generalist, kept so the fan-out can
# be turned off without losing a working single-agent path.
AGENT_PROMPTS: dict[str, str] = {
    "reviewer": "code_review.md",
    "bug": "bug.md",
    "security": "security.md",
    "performance": "performance.md",
}


@lru_cache(maxsize=1)
def load_shared() -> str:
    """The skeleton alone, with the agent-specific slots removed.

    **This is the system prompt every agent sends, byte for byte.** Prompt caching is
    prefix-based: a request is served from cache only up to the first byte that differs
    from one already processed. Putting `load_prompt(agent)` in the system slot — which is
    position 0 — meant the three agents diverged before the repository bundle was even
    reached, so the shared bundle was never shared and each agent paid full price for it.

    The per-agent role and domain move to `load_agent_section`, which belongs in the
    trailing uncached block alongside the focus hints.
    """
    skeleton = (PROMPT_DIR / "_shared.md").read_text(encoding="utf-8")
    return skeleton.replace("{{ROLE}}\n\n", "").replace("{{DOMAIN}}\n\n", "").strip()


@lru_cache(maxsize=8)
def load_agent_section(agent: str) -> str:
    """This agent's role and taxonomy, for the uncached tail.

    Last in the request rather than first, which also puts it closest to the model's
    output — instructions at the end of a long context are followed at least as well as
    ones at the start.
    """
    filename = AGENT_PROMPTS.get(agent)
    if filename is None:
        raise KeyError(f"no prompt for agent {agent!r}; known: {sorted(AGENT_PROMPTS)}")
    role, _, domain = (PROMPT_DIR / filename).read_text(encoding="utf-8").partition(DOMAIN_MARKER)
    return f"{role.replace(ROLE_MARKER, '').strip()}\n\n{domain.strip()}"


@lru_cache(maxsize=8)
def load_prompt(agent: str = "reviewer") -> str:
    """The full system prompt for one agent.

    Cached: this is read on every call in a fan-out, and it is a pure function of the
    files on disk.
    """
    filename = AGENT_PROMPTS.get(agent)
    if filename is None:
        raise KeyError(f"no prompt for agent {agent!r}; known: {sorted(AGENT_PROMPTS)}")

    skeleton = (PROMPT_DIR / "_shared.md").read_text(encoding="utf-8")
    fragment = (PROMPT_DIR / filename).read_text(encoding="utf-8")

    role, _, domain = fragment.partition(DOMAIN_MARKER)
    role = role.replace(ROLE_MARKER, "").strip()
    domain = domain.strip()
    if not domain:
        raise ValueError(f"{filename} has no {DOMAIN_MARKER} section")

    return skeleton.replace("{{ROLE}}", role).replace("{{DOMAIN}}", domain)

#!/usr/bin/env python3
"""Headless sub-agent dispatch — the engine that lets the orchestrator run as
deterministic Python with NO AI brain.

It replaces the in-session Agent tool: each sub-agent becomes a `claude -p`
subprocess whose
  - system prompt  = the agent definition body (.claude/agents/<name>.md, frontmatter stripped)
  - allowed tools  = the agent's `tools:` frontmatter line
  - model          = the agent's `model:` frontmatter (default haiku)
and a pool of them runs in parallel. Sub-agents write their own output files
(candidates-batch / enrich-out / emails-drafted), exactly as under the Agent
tool, so downstream stages are unchanged.

Quality is identical to the Agent-tool path: same prompt + tools + model + input,
same underlying WebSearch/WebFetch implementation. Validated head-to-head
2026-06-24 (same person/role/gender/basis; protocol rules — EnrichPhone gate,
verbatim>inferred, found:false-on-no-email — obeyed identically). Per-lead
differences are inherent LLM variance, present within either mechanism.

Auth note: uses the logged-in Claude Code subscription (OAuth) — do NOT pass
--bare (that forces ANTHROPIC_API_KEY, which this stack does not set). Tools are
allowed via --allowedTools (the intended headless allowlist), NOT
--permission-mode bypassPermissions.
"""
from __future__ import annotations
import concurrent.futures
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]          # .../lead-outreach-system
AGENTS_DIR = REPO / ".claude" / "agents"            # cwd-level agent definitions


def _agent_md(name: str) -> str:
    p = AGENTS_DIR / f"{name}.md"
    if not p.exists():
        raise FileNotFoundError(f"agent definition missing: {p}")
    return p.read_text()


# Optional blocks in an agent definition, fenced by
#   <!-- OPTIONAL:<key> -->  …  <!-- /OPTIONAL:<key> -->
# and dropped from the dispatched system prompt unless the caller opts in.
# The name-finder's phone ladder is half its 17 KB body and is dead weight on an
# email-only run (EnrichPhone was `no` on 208/208 batches of the last fire), yet
# it shipped as system prompt on every one of those dispatches.
_OPTIONAL_RE = re.compile(
    r"[ \t]*<!--\s*OPTIONAL:(?P<key>[\w-]+)\s*-->.*?<!--\s*/OPTIONAL:(?P=key)\s*-->[ \t]*\n?",
    re.S)


def agent_body(name: str, include: set[str] | None = None) -> str:
    """The instructions = everything after the YAML frontmatter.

    `include` names the OPTIONAL blocks to keep; every other optional block is
    stripped. None/empty keeps nothing optional."""
    md = _agent_md(name)
    parts = md.split("---", 2)
    body = (parts[2] if len(parts) >= 3 else md).strip()
    keep = include or set()
    return _OPTIONAL_RE.sub(
        lambda m: m.group(0) if m.group("key") in keep else "", body).strip()


def agent_tools(name: str) -> str:
    m = re.search(r"^tools:\s*(.+)$", _agent_md(name), re.M)
    return ",".join(t.strip() for t in m.group(1).split(",")) if m else ""


def agent_model(name: str) -> str:
    m = re.search(r"^model:\s*(\S+)", _agent_md(name), re.M)
    return m.group(1).strip() if m else "haiku"


def dispatch_one(agent: str, prompt: str, *, timeout: int = 300,
                 cwd: str | None = None,
                 include: set[str] | None = None) -> tuple[int, str, str]:
    """Run ONE sub-agent headless. Returns (returncode, stdout, stderr).
    The agent writes its result file itself; stdout is just its terse 'Done:' line."""
    cmd = [
        "claude", "-p", prompt,
        "--append-system-prompt", agent_body(agent, include),
        "--allowedTools", agent_tools(agent),
        "--model", agent_model(agent),
        "--output-format", "text",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd or str(REPO))
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"


def dispatch_pool(agent: str, prompts: list[str], *, max_workers: int = 12,
                  timeout: int = 300, cwd: str | None = None,
                  on_done=None, include: set[str] | None = None) -> list[tuple[int, str, str]]:
    """Run `agent` over each prompt in `prompts`, up to max_workers in parallel.
    Mirrors the Agent-tool fan-out (one agent per query / per lead). Returns
    results in input order. `on_done(i, rc, out)` is called as each finishes."""
    results: list[tuple[int, str, str] | None] = [None] * len(prompts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        fut = {ex.submit(dispatch_one, agent, p, timeout=timeout, cwd=cwd,
                         include=include): i
               for i, p in enumerate(prompts)}
        for f in concurrent.futures.as_completed(fut):
            i = fut[f]
            results[i] = f.result()
            if on_done:
                on_done(i, results[i][0], results[i][1])
    return results  # type: ignore[return-value]


if __name__ == "__main__":
    # Self-test: confirm a definition loads + a single dispatch returns rc=0.
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()
    print(f"agent={a.agent} model={agent_model(a.agent)} tools={agent_tools(a.agent)}")
    rc, out, err = dispatch_one(a.agent, a.prompt, timeout=a.timeout)
    print(f"rc={rc}\n--- stdout ---\n{out[-600:]}\n--- stderr ---\n{err[-300:]}")

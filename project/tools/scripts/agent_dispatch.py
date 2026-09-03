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
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]          # .../lead-outreach-system
AGENTS_DIR = REPO / ".claude" / "agents"            # cwd-level agent definitions


def foreign_memory_files() -> list[Path]:
    """CLAUDE.md files ABOVE the repo that `claude -p` would auto-discover.

    Every dispatch runs with cwd=REPO, and Claude Code walks the cwd's parent
    chain for CLAUDE.md — so a memory file belonging to some unrelated project
    that happens to sit in a parent directory is loaded into the system prompt
    of EVERY sub-agent, once per lead.

    This is not hypothetical: until 2026-08-27 this checkout lived under
    ~/Downloads next to a 40 KB `CLAUDE.md` for an ophthalmology website, so
    ~10k tokens of eye-surgery brochure copy shipped with all 237 name-finders
    of every fire (~2.4M tokens/run) and was never read by anything. The fix
    was to give that project its own folder; this function makes the next
    occurrence loud instead of silent."""
    return [p for d in REPO.resolve().parents
            if (p := d / "CLAUDE.md").is_file()]


def nested_memory_files(path: Path) -> list[Path]:
    """CLAUDE.md files BETWEEN the repo root and `path` that a `Read` of that
    path would pull into the agent's context as `nested_memory`.

    Claude Code loads every CLAUDE.md on the directory chain from cwd down to a
    file the agent reads. foreign_memory_files() only looks ABOVE the repo; this
    is the other direction, and it is where the real cost was hiding. Measured
    on 2026-09-02-gcc-receptionist: project/CLAUDE.md (30 KB, ~7.5k tokens) was
    attached to 664/665 name-finder transcripts the moment each agent Read its
    batch file under project/runs/<slug>/ — ~5M tokens per fire that no agent
    ever needed. The batch files now live in <repo>/.fire-work/<slug>/, and
    this check is the regression guard for exactly that bug: the walk stops at
    (and excludes) the repo root, so the repo's own CLAUDE.md never trips it."""
    p = Path(path).resolve()
    d = p if p.is_dir() else p.parent
    root = REPO.resolve()
    found = []
    while True:
        if d == root or d == d.parent:
            break
        if (d / "CLAUDE.md").is_file():
            found.append(d / "CLAUDE.md")
        d = d.parent
    return found


_INPUT_FILE_RE = re.compile(r"^Your input file:\s*(.+)$", re.M)


def check_prompt_context(prompts: list[str]) -> None:
    """Abort BEFORE any process is spawned if a prompt points an agent at a
    file whose Read would drag a nested CLAUDE.md into its context.

    Only the path-based prompts ("Your input file: <abs path>") are checked:
    that path is the one file every such agent is guaranteed to Read. Inline
    prompts name no file to walk from."""
    seen_dirs: set[Path] = set()
    for pr in prompts:
        m = _INPUT_FILE_RE.search(pr)
        if not m:
            continue
        d = Path(m.group(1).strip()).resolve().parent
        if d in seen_dirs:
            continue
        seen_dirs.add(d)
        hits = nested_memory_files(d)
        if hits:
            listing = "; ".join(f"{p} ({p.stat().st_size // 1024} KB)" for p in hits)
            raise RuntimeError(
                f"batch files under {d} sit below {len(hits)} CLAUDE.md file(s) that "
                f"every agent Read of them would load as nested memory: {listing}. "
                f"Per-agent batch/out files belong in <repo>/.fire-work/<slug>/ "
                f"(run_fire passes --work-dir to every prep/merge script) — a batch "
                f"file inside project/ is the 2026-09-02 bug coming back.")


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


def session_flags() -> list[str]:
    """Isolation flags every headless session gets (verified against
    `claude --help`, 2.1.257).

    Measured on 2026-09-02-gcc-receptionist (663 agents, transcripts read):
    every agent WROTE ~13.7k tokens of per-session context — the user-level
    hooks from ~/.claude/settings.json (a prompt-type SessionStart hook and a
    PostToolUse git hook), a ~60-skill listing, the account's MCP connectors
    (two of which fail to connect on every dispatch), and git status — and
    because cwd/env/git-status differ per session, all 663 wrote their ~18k
    token prefix instead of reading it from cache. None of it is used by a
    sub-agent: they have four tools, no MCP, no skills.

      --setting-sources project      project settings still grant WebSearch;
                                     user hooks are skipped
      --strict-mcp-config            no MCP servers (none configured => none)
      --disable-slash-commands       no skills listing in the prompt
      --exclude-dynamic-system-prompt-sections
                                     cwd/env/git-status/memory-paths leave the
                                     system prompt, so every agent shares ONE
                                     cacheable prefix
      --effort <AGENT_EFFORT|low>    thinking was ~70% of output tokens; the
                                     operator compares `survived` on the next
                                     fire and can set AGENT_EFFORT=medium

    NOT --no-session-persistence: the transcripts are how all of this was
    measured, and they stay the audit trail."""
    return [
        "--setting-sources", "project",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--exclude-dynamic-system-prompt-sections",
        "--effort", os.environ.get("AGENT_EFFORT", "low"),
    ]


def dispatch_one(agent: str, prompt: str, *, timeout: int = 300,
                 cwd: str | None = None,
                 include: set[str] | None = None) -> tuple[int, str, str]:
    """Run ONE sub-agent headless. Returns (returncode, stdout, stderr).
    The agent writes its result file itself; stdout is just its terse 'Done:' line."""
    tools = agent_tools(agent)
    # --allowedTools takes permission PATTERNS ("Bash(git *)"); --tools takes bare
    # built-in NAMES. Every agent today lists bare names, but strip any pattern
    # suffix for --tools so adding a scoped-permission agent later cannot silently
    # hand the CLI an unparseable tool name.
    tool_names = ",".join(sorted({t.split("(")[0].strip() for t in tools.split(",") if t.strip()}))
    cmd = [
        "claude", "-p", prompt,
        "--append-system-prompt", agent_body(agent, include),
        # --tools LOADS this exact set; --allowedTools only PERMITS it. Without
        # --tools the CLI ships the whole built-in roster and defers most of it,
        # so the agent must spend a ToolSearch round-trip to get WebSearch and
        # WebFetch back before it can do its job. Measured on 2026-09-02-eu-
        # hotels-2: 106 of 110 agents made that call (113 in all), and turn-1
        # context was 37,074 tokens vs 11,924 with --tools — a ~25k delta that
        # is re-read on EVERY turn (~66M cache-read tokens across a 110-agent
        # fire). Both flags stay: one sizes the context, the other the sandbox.
        "--tools", tool_names,
        "--allowedTools", tools,
        "--model", agent_model(agent),
        "--output-format", "text",
        *session_flags(),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=cwd or str(REPO))
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s"


def preflight(timeout: int = 90) -> tuple[bool, str]:
    """One throwaway `claude -p` on the name-finder's model with no tools.

    Proves the headless path can reach the model BEFORE a fire spends ledger
    ground. On 2026-09-01-gcc-receptionist every one of 247 name-finders came
    back `Not logged in · Please run /login`; the abort then retired 238 domains
    as "proven unreachable" and burned Dammam off the gmaps ledger for six
    verticals. A later probe on the same machine returned `Claude Code 2.1.131
    does not support this model; version 2.1.251 or newer is required` with
    rc=0 — so the stdout is checked, not just the exit code.

    Runs with the same session_flags() as a real dispatch, so a flag the
    installed CLI does not accept fails HERE, not 250 times in Stage 5.5."""
    cmd = ["claude", "-p", "Reply with exactly the word OK and nothing else.",
           "--tools", "", "--allowedTools", "", "--max-turns", "1",
           "--model", agent_model("name-finder"), "--output-format", "text",
           *session_flags()]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(REPO))
    except subprocess.TimeoutExpired:
        return False, f"no answer within {timeout}s"
    except FileNotFoundError:
        return False, "`claude` is not on PATH"
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if r.returncode == 0 and "ok" in out.lower() and "error" not in out.lower():
        return True, out
    return False, (out or err or f"rc={r.returncode}")[-300:]


def dispatch_pool(agent: str, prompts: list[str], *, max_workers: int = 12,
                  timeout: int = 300, cwd: str | None = None,
                  on_done=None, include: set[str] | None = None) -> list[tuple[int, str, str]]:
    """Run `agent` over each prompt in `prompts`, up to max_workers in parallel.
    Mirrors the Agent-tool fan-out (one agent per query / per lead). Returns
    results in input order. `on_done(i, rc, out)` is called as each finishes.

    Raises RuntimeError before spawning anything if a prompt's input file sits
    below a nested CLAUDE.md (see check_prompt_context)."""
    check_prompt_context(prompts)
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
    check_prompt_context([a.prompt])
    rc, out, err = dispatch_one(a.agent, a.prompt, timeout=a.timeout)
    print(f"rc={rc}\n--- stdout ---\n{out[-600:]}\n--- stderr ---\n{err[-300:]}")

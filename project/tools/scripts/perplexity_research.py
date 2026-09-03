#!/usr/bin/env python3
"""Stage 5.5/8.6 research provider — Perplexity via OpenRouter.

WHAT THIS REPLACES
The research half of the `name-finder` / `li-finder` fan-out. Those agents are
headless `claude -p` sessions that search the web with WebSearch/WebFetch. A
search-grounded model does the same job in ONE request with no agent loop.

Measured on 2026-09-02-eu-hotels-2 (110 agents, transcripts read):
  headless name-finder   10 API calls/lead, ~15k prefix RE-READ each turn,
                         ~220k tokens and ~105 s per lead
  perplexity/sonar       1 request, ~1-3k tokens, ~3-15 s per lead

WHY IT WRITES enrich-out-NNN.json AND NOTHING ELSE
The batch file contract (enrich-batch-NNN.txt in, enrich-out-NNN.json out) is
the ONLY thing Stage 5.5 shares with its agents. Emitting the same file means
`enrich_contact_person.py --phase merge` — the direct-email gate, the salutation
contract, the SMTP rescue, the drop ledger — runs completely unchanged. No
stage downstream can tell which provider produced the JSON.

THE DIVISION OF LABOUR IS MEASURED, NOT ASSUMED
Scored against this pipeline's own labelled winners (the 8 leads Stage 5.5 sent
on 2026-09-02-eu-hotels-2), with full SitePages + harvested addresses supplied:

    same decision-maker identified      5/8
    email passing is_direct_email()     1/8
    both (would survive the merge)      1/8

So Perplexity is strong on the PERSON and weak on the ADDRESS: on four leads it
named the right person (Camichel, Amberger, Zurbrügg, Vialmin) and returned no
usable email, while the deterministic path had already produced all four
(pattern_inferred / verbatim / smtp_verified). Once it returned
`artboutique@monopol.ch` — a generic mailbox — labelled `verbatim`/`high`.

Hence the prompt below FORBIDS constructing an address. A name with no email is
exactly the shape phase_merge's SMTP rescue consumes (`rescue_plan` is built
from leads with first/last and no email), so `found:false` + the structured name
fields hands the address question to smtp_email_probe.py — free, RCPT-verified,
and the source of the smtp_verified wins. Guessing here would instead trip the
kill-on-fallback gate and lose the lead.

COST
Billed per REQUEST (~$0.005), not meaningfully per token: 16 calls = $0.0876.
~$0.46 for an 84-lead fire, ~$1.70 for a 337-batch gcc-receptionist fire. This
is a paid API and a deliberate exception to the free-only rule, authorised by
the operator supplying OPEN_ROUTER_API_KEY (project/.env) for this purpose.
Sourcing stays free.
"""
from __future__ import annotations
import concurrent.futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = os.environ.get("PERPLEXITY_MODEL", "perplexity/sonar")

# The site context the batch file carries is the highest-value part of the
# prompt (most wins are answered by it without any search), but SitePages is
# also the only unbounded field. Cap it: the about/team/contact text that names
# a decision-maker is always near the top of those pages.
MAX_SITEPAGES_CHARS = int(os.environ.get("PERPLEXITY_SITEPAGES_CHARS", "6000"))


def api_key() -> str:
    """OPEN_ROUTER_API_KEY from project/.env (falls back to the environment)."""
    for name in ("OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY"):
        if os.environ.get(name):
            return os.environ[name].strip()
    env = PROJECT / ".env"
    if env.exists():
        found = dict(re.findall(r"^([A-Z_0-9]+)=(.*)$", env.read_text(), re.M))
        for name in ("OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY"):
            if found.get(name):
                return found[name].strip()
    raise SystemExit(
        "ABORT: no OPEN_ROUTER_API_KEY in project/.env or the environment. "
        "Stage 5.5 is set to RESEARCH_PROVIDER=perplexity but cannot authenticate.")


# Tailored to a SEARCH-GROUNDED model, not to an agent: no tool names, no file
# paths, no workflow ladder — Perplexity searches on its own and cannot Read or
# Write. What survives from name-finder.md is only what changes the ANSWER: who
# counts as the decision-maker, what counts as a direct email, and the exact
# output schema the merge parses.
SYSTEM_PROMPT = """You identify the decision-maker of ONE business.

TARGET (in priority order): Owner, Founder, CEO, Managing Director, General
Manager, Director, Proprietor. Prefer the most senior named human. Never a
receptionist, a PR contact, or a junior employee. If the input names TargetRoles,
those override this ladder.

You are given SitePages (text already scraped from the company's own website)
and any emails harvested from it. READ THOSE FIRST — the answer is usually
already there. Search the web only for what they do not answer.

EMAIL — read this carefully, it is where this task is usually got wrong:
- Return an address ONLY if you saw that exact address, in full, in a source.
  Set email_basis "verbatim" and put the URL you saw it at in email_source_url.
- A direct email belongs to a NAMED PERSON: firstname@, firstname.lastname@,
  f.lastname@, firstname.l@.
- NEVER return info@, contact@, hello@, office@, reception@, empfang@, kontakt@,
  booking@, bookings@, reservations@, admin@, sales@, welcome@, stay@, or any
  brand/department mailbox. These are NOT direct emails. A shared mailbox is
  worse than no answer.
- DO NOT CONSTRUCT, GUESS, PATTERN-MATCH OR INFER AN ADDRESS. If you did not see
  it verbatim, leave email "" and set found false. A separate verification step
  handles address construction; a guess from you is discarded and loses the lead.

NAME AND GENDER:
- first_name and last_name properly capitalised (Al Sayed, O'Connor, McDonald);
  particles al/el/bin/de/van/von lowercase.
- title is exactly "Mr." or "Mrs." — never Ms., Dr. or Eng. Assign it from the
  person's own pronouns, photo or first name. If a surname is set you must set
  title. If gender is genuinely ambiguous, give first_name only and title "".
- role EXACTLY as written in the source. Never invent or inflate it.
- Never turn the business name into a surname: "Ferrari Dental" does not make
  the owner "Mr. Ferrari" unless a source names a real human that way.

FOUND THE PERSON BUT NOT A VERBATIM EMAIL? That is a NORMAL, USEFUL result and
the most common one. Return found false WITH first_name, last_name, title, role
and source_url filled in, plus a short reason. Downstream verification needs
exactly those fields, so never blank them just because the email failed.

Reply with ONE JSON object and nothing else — no markdown fence, no commentary:
{"lead_id":"<echo exactly>","found":true|false,"first_name":"","last_name":"",
"title":"Mr."|"Mrs."|"","role":"","email":"","email_basis":"verbatim"|"",
"source_url":"","email_source_url":"","confidence":"high"|"medium",
"reason":"required when found is false"}

email_source_url must be a bare http(s) URL and nothing else."""


def parse_batch(text: str) -> dict:
    """Pull the fields out of an enrich-batch-NNN.txt. Same file the agent reads."""
    out: dict[str, str] = {}
    head, _, pages = text.partition("SitePages")
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    body = pages.partition("\n")[2].strip() if pages else ""
    out["_sitepages"] = body[:MAX_SITEPAGES_CHARS]
    return out


def build_user_prompt(b: dict) -> str:
    keep = [("LeadId", b.get("LeadId", "")), ("Business", b.get("Business", "")),
            ("Country", b.get("Country", "")), ("Vertical", b.get("Vertical", "")),
            ("Website", b.get("Website", "")),
            ("SitePersonalEmails", b.get("SitePersonalEmails", "(none found on site)")),
            ("SiteRoleEmails", b.get("SiteRoleEmails", "(none)"))]
    if b.get("TargetRoles"):
        keep.append(("TargetRoles", b["TargetRoles"]))
    lines = "\n".join(f"{k}: {v}" for k, v in keep if v)
    pages = b.get("_sitepages") or "(none scraped)"
    return (f"{lines}\n\nSitePages (already scraped from this company's own site):\n{pages}\n\n"
            f"Identify the decision-maker. Return the JSON object.")


_JSON = re.compile(r"\{.*\}", re.S)


def _local_carries_name(local: str, first: str, last: str) -> bool:
    """Does this local-part actually belong to THIS person?

    A direct email is the decision-maker's own mailbox, so its local part
    carries their name: `dominik`, `d.zurbruegg`, `zurbruegg`, `dz`. Measured
    need for this: on the labelled set the model returned
    `artboutique@monopol.ch` for Dominik Zurbrügg, called it `verbatim` with
    `high` confidence, and it PASSES is_direct_email() because "artboutique" is
    not in that gate's generic-local list. A brand mailbox reaching the send
    step is exactly what kill-on-fallback forbids, so the provider proves the
    link itself instead of trusting either the model or the downstream gate."""
    l = re.sub(r"[^a-z]", "", _ascii(local))
    if not l:
        return False
    # Try BOTH foldings of the name: plain ASCII strip (Zurbrügg -> zurbrugg)
    # and the German/Nordic transliteration mailboxes actually use
    # (Zurbrügg -> zurbruegg). Missing the second one rejected a legitimate
    # `d.zurbruegg@` on the labelled set, and CH/AT/DE are a large share of the
    # eu-hotels footprint.
    for fold in (_ascii, _translit):
        f = re.sub(r"[^a-z]", "", fold(first))
        s = re.sub(r"[^a-z]", "", fold(last))
        if f and len(f) > 2 and f in l:
            return True
        if s and len(s) > 2 and s in l:
            return True
        # initial + surname (dzurbruegg), surname + initial, f.last styles
        if f and s and len(s) > 2 and (f[0] + s in l or s + f[0] in l):
            return True
    return False


def _ascii(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFKD", (s or "").lower()).encode("ascii", "ignore").decode()


_TRANSLIT = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
             "å": "aa", "æ": "ae", "ø": "oe"}


def _translit(s: str) -> str:
    out = (s or "").lower()
    for a, b in _TRANSLIT.items():
        out = out.replace(a, b)
    return _ascii(out)


def normalise(out: dict) -> dict:
    """Force the model's reply into the contract Stage 5.5's merge relies on.

    TWO rules, both learned from a measured failure on the labelled set:

    1. `found:true` MUST carry a usable email. phase_merge only builds its
       `rescue_plan` from results where `found` is falsey, so a `found:true`
       with an empty address SKIPS the SMTP rescue and then dies at the email
       gate — the lead is lost precisely where the free, RCPT-verified rescue
       would have saved it. 7 of 8 replies on the labelled set had exactly this
       shape.
    2. Only a VERBATIM address the model actually saw is kept, and only if its
       local part carries the person's name. Anything else is cleared so the
       address question falls to smtp_email_probe.py, which constructs and
       RCPT-verifies properly (the source of the smtp_verified wins).

    The name fields are always preserved — they are what the rescue needs."""
    email = (out.get("email") or "").strip().lower()
    first = (out.get("first_name") or "").strip()
    last = (out.get("last_name") or "").strip()
    why = []
    if email:
        local = email.split("@")[0]
        if out.get("email_basis") not in ("verbatim", ""):
            why.append(f"non-verbatim email {email!r} (basis {out.get('email_basis')!r})")
        elif not _local_carries_name(local, first, last):
            why.append(f"{email!r} does not carry this person's name — shared/brand mailbox")
    if why:
        out["reason"] = ((out.get("reason") or "") +
                         f" [provider dropped {why[0]}; address construction is the "
                         f"SMTP rescue's job]").strip()
        email = ""
        out["email_basis"] = ""
    out["email"] = email
    # A result with no address is NOT a find — it must reach the rescue path.
    out["found"] = bool(email) and bool(first or last)
    return out


def _post(payload: dict, key: str, timeout: int) -> dict:
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def research_one(batch_path: Path, key: str, *, model: str = DEFAULT_MODEL,
                 timeout: int = 180, retries: int = 3) -> tuple[dict, float]:
    """One lead -> (result dict in the name-finder schema, cost in USD)."""
    b = parse_batch(batch_path.read_text())
    lead_id = b.get("LeadId", "")
    payload = {"model": model, "temperature": 0,
               "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": build_user_prompt(b)}]}
    last = ""
    for attempt in range(retries):
        try:
            d = _post(payload, key, timeout)
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            # 429/5xx are worth another go; a 4xx auth/quota error is not.
            if e.code not in (408, 409, 429, 500, 502, 503, 504):
                break
            time.sleep(2 * (attempt + 1))
            continue
        except Exception as e:                       # network/timeout
            last = str(e)[:120]
            time.sleep(2 * (attempt + 1))
            continue
        content = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
        cost = float((d.get("usage") or {}).get("cost") or 0.0)
        m = _JSON.search(re.sub(r"```(?:json)?", "", content))
        if not m:
            last = "no JSON in reply"
            continue
        try:
            out = json.loads(m.group(0))
        except json.JSONDecodeError:
            last = "malformed JSON"
            continue
        out["lead_id"] = lead_id                      # never trust the model to echo it
        return normalise(out), cost
    return ({"lead_id": lead_id, "found": False,
             "reason": f"perplexity research failed after {retries} attempt(s): {last}"}, 0.0)


def run_batches(work_dir: Path, *, model: str = DEFAULT_MODEL, max_workers: int = 8,
                timeout: int = 180, on_done=None) -> dict:
    """Every enrich-batch-NNN.txt in work_dir -> enrich-out-NNN.json beside it."""
    key = api_key()
    batches = sorted(work_dir.glob("enrich-batch-*.txt"))
    if not batches:
        return {"n": 0, "found": 0, "cost": 0.0, "elapsed": 0.0}
    t0 = time.monotonic()
    n_found = 0
    cost = 0.0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(research_one, b, key, model=model, timeout=timeout): b
                for b in batches}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            b = futs[fut]
            res, c = fut.result()
            cost += c
            n_found += bool(res.get("found"))
            out = b.parent / b.name.replace("enrich-batch-", "enrich-out-").replace(".txt", ".json")
            out.write_text(json.dumps(res, ensure_ascii=False) + "\n")
            if on_done:
                on_done(i, len(batches), res)
    named = 0
    for b in batches:
        p = b.parent / b.name.replace("enrich-batch-", "enrich-out-").replace(".txt", ".json")
        try:
            d = json.loads(p.read_text())
            named += bool((d.get("first_name") or "").strip() or (d.get("last_name") or "").strip())
        except Exception:
            pass
    # `found` is post-normalise, so it counts leads carrying a KEPT verbatim
    # address — not merely ones where a person was identified. `named` minus
    # `found` is the population the merge's SMTP rescue then works on.
    return {"n": len(batches), "found": n_found, "named": named,
            "cost": cost, "elapsed": time.monotonic() - t0}


def annotate_batches(work_dir: Path, *, model: str = DEFAULT_MODEL, max_workers: int = 8,
                     timeout: int = 180) -> dict:
    """Name the decision-maker for every batch, then write that name back INTO
    the batch file as a `KnownDecisionMaker:` block. Writes NO enrich-out files —
    the agents still run, but arrive already knowing who the person is.

    THIS IS THE SPLIT THE MEASUREMENTS FORCED. Head-to-head on the 133 identical
    batch files of 2026-09-04-eu-hotels:

                    named          address passing is_direct_email
        agent        91 (68%)       39
        perplexity  106 (80%)        5     (117 named / still 5 when the prompt
                                            was relaxed to allow reconstruction)

    Perplexity names MORE people than the agent — 27 that the agent never
    identified — and it does it in 91 s for $0.82 instead of ~10 API calls per
    lead. But it will not hunt an address: it returned none 125 times out of 133
    even when explicitly permitted to rebuild one from a mask or a colleague's
    address. The agent's 39 come from off-site digging (masks, directories,
    press) that neither Perplexity nor deterministic code reproduces —
    build_from_site_format() closed 4 of 101, because 97 of those domains
    publish no personal-email format at all.

    So each side does the half it wins, and the agent's remaining job is
    strictly smaller: identity is handed to it, so its searches go to the
    address instead of the person."""
    key = api_key()
    batches = sorted(work_dir.glob("enrich-batch-*.txt"))
    if not batches:
        return {"n": 0, "named": 0, "cost": 0.0, "elapsed": 0.0}
    t0 = time.monotonic()
    cost = 0.0
    named = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(research_one, b, key, model=model, timeout=timeout): b
                for b in batches}
        for fut in concurrent.futures.as_completed(futs):
            b = futs[fut]
            res, c = fut.result()
            cost += c
            first = (res.get("first_name") or "").strip()
            last = (res.get("last_name") or "").strip()
            if not (first or last):
                continue
            named += 1
            # Inserted before the SitePages block so it reads as part of the
            # lead's header, not as scraped page text.
            text = b.read_text()
            block = (f"KnownDecisionMaker: {first} {last}".rstrip() + "\n"
                     f"KnownRole: {(res.get('role') or '').strip()}\n"
                     f"KnownTitle: {(res.get('title') or '').strip()}\n"
                     f"KnownSourceUrl: {(res.get('source_url') or '').strip()}\n"
                     f"KnownEmail: {(res.get('email') or '').strip()}\n")
            marker = "\nSitePages"
            if marker in text:
                head, _, tail = text.partition(marker)
                b.write_text(head + block + marker + tail)
            else:
                b.write_text(text + block)
    return {"n": len(batches), "named": named, "cost": cost,
            "elapsed": time.monotonic() - t0}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Perplexity research provider for Stage 5.5.")
    ap.add_argument("--work-dir", required=True, help="dir holding enrich-batch-*.txt")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()

    def _cb(i, n, res):
        who = f"{res.get('first_name','')} {res.get('last_name','')}".strip() or "-"
        print(f"  [{i}/{n}] {res.get('lead_id','?')}: found={bool(res.get('found'))} {who}",
              flush=True)

    s = run_batches(Path(a.work_dir).resolve(), model=a.model,
                    max_workers=a.max_workers, timeout=a.timeout)
    print(f"Perplexity research: {s['n']} lead(s) in {s['elapsed']:.0f}s — "
          f"{s.get('named', 0)} named, {s['found']} with a verbatim email, "
          f"${s['cost']:.4f}")

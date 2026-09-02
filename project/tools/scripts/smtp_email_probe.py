#!/usr/bin/env python3
"""SMTP pattern-guess + RCPT-verification email finder.

WHAT THIS IS
Given a person's name and a company's mail domain (both of which this pipeline
already has by the time this runs — the name from name-finder's search, the
domain from the lead's own website/email), generate the common corporate
email-address patterns, then ask the domain's own mail server which one (if
any) it actually accepts, via a single read-only SMTP RCPT TO probe per
domain. No email is ever sent — MAIL FROM/RCPT TO/RSET is a query, not a
delivery. Same technique commercial tools (Hunter, Clearbit, etc.) use; this
is the free, self-hosted, protocol-level version of it.

This does NOT scrape LinkedIn, any website, or any third-party API. Its only
inputs are a name and a domain, which this pipeline already resolved for
every "name found, no email" drop (Stage 5.5's `leads-dropped.json`).

WHY ONE SMTP SESSION PER DOMAIN, NOT ONE PER CANDIDATE (fixed 2026-08-20)
The reference version this was built from opened a fresh TCP connection + full
SMTP handshake for every single candidate address (up to ~12 per person) and
for the separate catch-all probe — 13 connections to the SAME mail server in a
tight loop with no pacing. Measured against three real domains from this
pipeline (arcotel.com, hotelscherer.at, dollinger.at) this is unnecessary and
risky: SMTP supports multiple RCPT TO commands inside ONE MAIL FROM
transaction, each answered with its own per-recipient code, which is exactly
what a legitimate multi-recipient send looks like — not the rapid-reconnect
pattern that gets an IP rate-limited or greylisted by the receiving server's
own abuse detection. This version opens ONE connection per domain: one
MAIL FROM, then RCPT TO the catch-all probe address followed by every real
candidate, all in the same transaction, then RSET + QUIT. Verified live: this
correctly told real from fake on hotelscherer.at, and correctly detected
accept-all behaviour on arcotel.com (Barracuda gateway) and dollinger.at
(M365 tenant without Directory-Based Edge Blocking) rather than reporting a
false verified hit on either.

REAL-WORLD LIMITATIONS (measured, not assumed)
* Some domains — especially behind enterprise email security gateways
  (Barracuda, Proofpoint, Mimecast) or M365 tenants without per-recipient
  validation enabled — accept RCPT TO for ANY address ("catch-all"). This
  script detects that (a random 16-char local part gets probed in the same
  transaction) and reports `catch_all`, never a false `verified`. Expect a
  real, non-trivial fraction of business domains to land here — this is not
  a bug, it's what those mail servers actually do.
* A `verified` (250) result means the mail server currently accepts that
  recipient at SMTP time. It is strong signal, not an absolute guarantee —
  a small share of servers accept-then-bounce later. Brevo's own bounce-sync
  (Stage 7) is still the backstop that catches anything this got wrong.
* This NEVER overrides the pipeline's own direct-email gate
  (`is_direct_email` in enrich_contact_person.py). A pattern can verify by
  SMTP purely by initials coincidence (e.g. "gm@" for someone whose initials
  happen to be G.M.) while actually being a ROLE mailbox (General Manager) —
  the semantic gate downstream is what catches that, this script only
  answers "does the mail server accept this string", nothing about whose
  mailbox it actually is.

COMPLIANCE
Finding an address this way is not permission to email it outside this
pipeline's existing rules — every result still passes through
is_direct_email(), domain_accepts_mail(), and the sent-log/suppression
ledgers exactly like any other resolved contact.

LIBRARY USE (the path the orchestrator uses)
    from smtp_email_probe import probe_person
    result = probe_person("Anna", "Muster", "example.com",
                           mail_from="you@yourdomain.com", timeout=10)
    # {"email": "...", "status": "verified|catch_all|unverified|not_found|guess|no_candidates",
    #  "confidence": "high|medium|low|none", "method": "smtp|pattern", "note": "..."}

CLI (standalone / debugging)
    python3 smtp_email_probe.py --in leads.csv --out results.csv
Input CSV needs columns (case-insensitive): first_name, last_name, domain.
"""
from __future__ import annotations
import argparse
import csv
import random
import smtplib
import socket
import string
import sys
import time
import unicodedata
from collections import OrderedDict
from pathlib import Path

try:
    import dns.resolver
except ImportError:
    sys.exit("Missing dependency. Install into the project venv with:\n"
             "  tools/venv/bin/pip install dnspython")


# Patterns ordered roughly by real-world corporate prevalence. {f}/{l} = first initials.
PATTERN_TEMPLATES = [
    "{first}.{last}",
    "{first}",
    "{f}{last}",
    "{first}{last}",
    "{first}_{last}",
    "{f}.{last}",
    "{first}{l}",
    "{last}.{first}",
    "{last}",
    "{last}{first}",
    "{f}{l}",
    "{first}-{last}",
]


def clean(part: str) -> str:
    """Fold accents to ASCII (José -> jose, Muñoz -> munoz), lowercase, a-z0-9 only."""
    part = (part or "").strip().lower()
    part = unicodedata.normalize("NFKD", part)
    part = "".join(ch for ch in part if not unicodedata.combining(ch))
    return "".join(ch for ch in part if ch in string.ascii_lowercase + string.digits)


def candidate_emails(first: str, last: str, domain: str) -> tuple[str, list[str]]:
    """Ordered, de-duplicated candidate addresses for a person at a domain."""
    f, l = clean(first), clean(last)
    domain = (domain or "").strip().lower().lstrip("@")
    if not f or not domain:
        return domain, []
    fields = {"first": f, "last": l, "f": f[:1], "l": l[:1] if l else ""}
    seen, out = set(), []
    for tpl in PATTERN_TEMPLATES:
        try:
            local = tpl.format(**fields)
        except (KeyError, IndexError):
            continue
        if (not local or local[0] in "._-" or local[-1] in "._-"
                or ".." in local or "__" in local or "--" in local):
            continue
        addr = f"{local}@{domain}"
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return domain, out


_mx_cache: dict[str, list[str]] = {}


def get_mx_hosts(domain: str) -> list[str]:
    """Mail exchanger hostnames for a domain, lowest-preference first. [] if none."""
    if domain in _mx_cache:
        return _mx_cache[domain]
    hosts: list[str] = []
    try:
        answers = dns.resolver.resolve(domain, "MX")
        ranked = sorted((r.preference, str(r.exchange).rstrip(".")) for r in answers)
        hosts = [h for _, h in ranked if h]
    except Exception:
        hosts = []
    _mx_cache[domain] = hosts
    return hosts


def _probe_domain(mx_host: str, mail_from: str, rcpts: list[str], timeout: float
                   ) -> dict[str, int | None]:
    """ONE SMTP session, ONE MAIL FROM, RCPT TO every address in `rcpts` in order.
    Returns {address: code_or_None}. A single connection failure fails every
    address in this batch with None (couldn't tell), not a false negative."""
    codes: dict[str, int | None] = {r: None for r in rcpts}
    try:
        server = smtplib.SMTP(timeout=timeout)
        server.connect(mx_host, 25)
        server.ehlo_or_helo_if_needed()
        code, _ = server.mail(mail_from)
        if code >= 400:
            # Server refused MAIL FROM itself (rare, e.g. SPF/policy) — every
            # RCPT in this session would be meaningless; leave all as None.
            try:
                server.quit()
            except Exception:
                pass
            return codes
        for r in rcpts:
            try:
                c, _ = server.rcpt(r)
                codes[r] = c
            except smtplib.SMTPServerDisconnected:
                break  # server closed the transaction mid-way; rest stay None
        try:
            server.rset()
            server.quit()
        except Exception:
            pass
    except (socket.timeout, socket.error, smtplib.SMTPException, OSError):
        pass
    return codes


def probe_person(first: str, last: str, domain: str, mail_from: str,
                  timeout: float = 10.0, do_verify: bool = True) -> dict:
    """Resolve one person to a best-guess email + status.
    Returns: {email, status, confidence, method, note}
      status: verified | catch_all | unverified | not_found | guess | no_candidates
    """
    domain, candidates = candidate_emails(first, last, domain)
    if not candidates:
        return {"email": "", "status": "no_candidates", "confidence": "none",
                "method": "-", "note": "missing name or domain"}

    top_guess = candidates[0]
    if not do_verify:
        return {"email": top_guess, "status": "guess", "confidence": "low",
                "method": "pattern", "note": "verification skipped"}

    mx = get_mx_hosts(domain)
    if not mx:
        return {"email": top_guess, "status": "unverified", "confidence": "low",
                "method": "pattern", "note": "no MX records / domain not resolvable"}
    mx_host = mx[0]

    junk_local = "".join(random.choices(string.ascii_lowercase, k=16))
    junk_addr = f"{junk_local}@{domain}"
    # ONE session covers the catch-all probe AND every real candidate.
    codes = _probe_domain(mx_host, mail_from, [junk_addr] + candidates, timeout)

    if all(c is None for c in codes.values()):
        return {"email": top_guess, "status": "unverified", "confidence": "low",
                "method": "pattern", "note": "SMTP unreachable (port 25 blocked/filtered, "
                                              "or the server refused this session)"}
    if codes.get(junk_addr) in (250, 251):
        return {"email": top_guess, "status": "catch_all", "confidence": "medium",
                "method": "pattern", "note": "domain accepts all mail; cannot verify individuals"}

    for addr in candidates:
        if codes.get(addr) in (250, 251):
            return {"email": addr, "status": "verified", "confidence": "high",
                    "method": "smtp", "note": "accepted by mail server (per-recipient RCPT)"}
    return {"email": "", "status": "not_found", "confidence": "none",
            "method": "smtp", "note": "mail server rejected every candidate"}


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def norm_headers(fieldnames):
    return {h.lower().strip(): h for h in (fieldnames or [])}


def run_csv(in_path: str, out_path: str, mail_from: str, delay: float,
            do_verify: bool, timeout: float) -> None:
    with open(in_path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)
        hmap = norm_headers(reader.fieldnames)

    required = ["first_name", "last_name", "domain"]
    missing = [c for c in required if c not in hmap]
    if missing:
        sys.exit(f"Input CSV is missing required column(s): {', '.join(missing)}\n"
                 f"Found columns: {', '.join(hmap.values()) or '(none)'}")

    passthrough = [h for key, h in hmap.items() if key not in required]
    out_fields = passthrough + ["found_email", "status", "confidence", "method", "note"]

    stats = OrderedDict((k, 0) for k in
                        ["verified", "catch_all", "unverified", "guess",
                         "not_found", "no_candidates"])
    last_domain = None

    with open(out_path, "w", newline="", encoding="utf-8") as out_fh:
        writer = csv.DictWriter(out_fh, fieldnames=out_fields)
        writer.writeheader()
        for i, row in enumerate(rows, 1):
            first = row.get(hmap["first_name"], "")
            last = row.get(hmap["last_name"], "")
            domain = row.get(hmap["domain"], "")
            if do_verify and last_domain is not None and domain != last_domain:
                time.sleep(delay)
            last_domain = domain

            res = probe_person(first, last, domain, mail_from, timeout, do_verify)
            stats[res["status"]] = stats.get(res["status"], 0) + 1

            out_row = {h: row.get(h, "") for h in passthrough}
            out_row.update({"found_email": res["email"], "status": res["status"],
                            "confidence": res["confidence"], "method": res["method"],
                            "note": res["note"]})
            writer.writerow(out_row)
            print(f"[{i}/{len(rows)}] {first} {last} @{domain} -> "
                  f"{res['email'] or '(none)'}  [{res['status']}]")

    print("\n=== Summary ===")
    for k, v in stats.items():
        if v:
            print(f"  {k:14s}: {v}")
    print(f"  {'total':14s}: {len(rows)}")
    print(f"\nWrote {out_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--in", dest="in_path", required=True)
    ap.add_argument("--out", dest="out_path", required=True)
    ap.add_argument("--from-addr", default=None,
                    help="MAIL FROM for probing. Default: BREVO_SENDER_EMAIL from .env "
                         "(a real, already-deliverable domain this pipeline already sends "
                         "from) — falls back to verify@example.com if unset.")
    ap.add_argument("--delay", type=float, default=1.5)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    mail_from = args.from_addr
    if not mail_from:
        env = {**load_env(Path(__file__).resolve().parents[2] / ".env")}
        mail_from = env.get("BREVO_SENDER_EMAIL") or "verify@example.com"

    run_csv(args.in_path, args.out_path, mail_from, args.delay,
            not args.no_verify, args.timeout)


if __name__ == "__main__":
    main()

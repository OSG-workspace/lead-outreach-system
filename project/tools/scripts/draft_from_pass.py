#!/usr/bin/env python3
"""
Generate per-lead drafts directly from a finished pass's artifacts.

Reads:
  runs/<slug>/leads-scored-pass<N>.json     (the scored funnel output)
  runs/<slug>/signals/<lead-id>.json        (the crawled signals)

Selects leads with funnel_status in {send_ready, signal_rescue, needs_signal}
that have a concrete signal in their JSON, merges the signal back into the
lead, runs them through funnel_lib.build_outreach_draft, and writes the
emails to runs/<slug>/emails-drafted-pass<N>.json plus a send-gate file
runs/<slug>/emails-eligible-pass<N>.json.

This is the recovery path when the main driver was interrupted, when the
funnel_lib drafting logic was updated after a run started, or when you want
to ship Pass 1 results before later passes finish.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "scripts"))

import funnel_lib as fl  # noqa: E402


_BOOKING_KEYWORDS = ("calendly", "acuity", "booksy", "fresha", "zenoti",
                     "book online", "schedule online", "online booking",
                     "reserve online")
_AI_KEYWORDS = ("artificial intelligence", " ai ", "ai-powered", "ai powered",
                "machine learning", "chatbot", "ai assistant", "automation")
_SAAS_KEYWORDS = ("hubspot", "salesforce", "zoho", "monday.com", "asana",
                  "microsoft 365", "office 365", "google workspace")
_HIRING_KEYWORDS = ("we're hiring", "we are hiring", "now hiring",
                    "join our team", "open positions", "careers", "vacancies")


def classify_signal(sig: dict) -> dict:
    out: dict = {"emails": sig.get("emails") or []}
    md = " ".join([
        sig.get("about_excerpt") or "",
        " ".join(sig.get("headings", {}).get("h1", []) or []),
        " ".join(sig.get("headings", {}).get("h2", []) or []),
        " ".join(sig.get("headings", {}).get("h3", []) or []),
        sig.get("meta_description") or "",
        sig.get("title") or "",
    ]).lower()

    if sig.get("has_booking_widget") or any(k in md for k in _BOOKING_KEYWORDS):
        out["mode"] = "booking_widget"
    elif sig.get("has_contact_form"):
        out["mode"] = "contact_form"
    elif sig.get("phones"):
        out["mode"] = "phone"

    if any(k in md for k in _AI_KEYWORDS):
        out["ai_mentioned"] = True
    if any(k in md for k in _SAAS_KEYWORDS):
        out["saas"] = True
    if any(k in md for k in _HIRING_KEYWORDS):
        out["hiring_manual"] = True
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--run-slug", required=True)
    p.add_argument("--pass-num", type=int, required=True)
    p.add_argument("--target", type=int, default=100)
    args = p.parse_args()

    run_dir = ROOT / "runs" / args.run_slug
    scored_file = run_dir / f"leads-scored-pass{args.pass_num}.json"
    sig_dir = run_dir / "signals"
    if not scored_file.exists():
        raise SystemExit(f"missing {scored_file}")

    cfg = fl.build_funnel_config(run_dir)
    leads = fl.load_jsonl(scored_file)

    # Merge signals back
    merged = []
    for lead in leads:
        lid = lead.get("id") or fl.slugify(lead.get("name"))
        sig_file = sig_dir / f"{lid}.json"
        if sig_file.exists():
            try:
                raw_sig = json.loads(sig_file.read_text())
                if raw_sig.get("fetched"):
                    lead["signals"] = classify_signal(raw_sig)
                    lead["primary_gap"] = fl.signal_used(lead) or lead.get("primary_gap")
            except json.JSONDecodeError:
                pass
        merged.append(lead)

    # Re-qualify with the freshly merged signals.
    ranked = sorted(merged, key=lambda r: -(r.get("score") or 0))
    pool = [l for l in ranked if l.get("funnel_status") in
            {"send_ready", "signal_rescue", "needs_signal"}]
    qres = fl.qualify_ranked_signals(pool, target=args.target * 2)
    qualified = qres["qualified"]
    print(f"merged signals into {len(merged)} scored leads")
    print(f"re-qualified: {len(qualified)}  drops={qres['drops']}")

    if not qualified:
        return

    # Generate drafts with the FRESH funnel_lib (whatever's currently on disk).
    drafts = [fl.build_outreach_draft(l, args.run_slug) for l in qualified]
    out_drafts = run_dir / f"emails-drafted-pass{args.pass_num}.json"
    fl.write_jsonl(out_drafts, drafts)

    eligible, gate_report = fl.gate_drafts(drafts, role_threshold=cfg.get("role_inbox_threshold", 85))
    out_eligible = run_dir / f"emails-eligible-pass{args.pass_num}.json"
    fl.write_jsonl(out_eligible, eligible)

    print(f"drafts written: {out_drafts}  count={len(drafts)}")
    print(f"send-gate:      {out_eligible}  eligible={len(eligible)}  dropped={gate_report['dropped']}")

    print()
    print("Top 5 eligible drafts:")
    for d in sorted(eligible, key=lambda r: -(r.get("score") or 0))[:5]:
        print(f"  score={d['score']:3}  signal={d.get('signal_used') or '?'}  "
              f"to={d.get('to_email','-')}  {d.get('to_name','')[:40]}")


if __name__ == "__main__":
    main()

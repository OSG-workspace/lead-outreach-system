#!/usr/bin/env python3
"""Balanced, reusable funnel guard for the outreach pipeline.

This script implements the markdown-defined route between sourcing and sending:

SOURCE -> source-fit filter -> FIND BEST EMAIL -> QUALIFY -> signal pool.

It intentionally does not scrape or send. It turns existing run artifacts into
bounded, explainable intermediate files that downstream personalization and
Brevo send steps can trust.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse


TARGET_COUNTRIES = ("AE", "SA", "QA", "BH", "KW")
TARGET_VERTICALS = ("clinic", "fitness", "fnb", "retail")
REAL_SIGNAL_KEYS = (
    "mode",
    "owner_name",
    "ai_mentioned",
    "saas",
    "branches",
    "hiring_manual",
    "pdf_menu",
)

COUNTRY_HINTS = {
    "AE": ("dubai", "abu dhabi", "sharjah", "ajman", "ras al khaimah",
           "fujairah", "umm al quwain", "uae", "united arab", "al ain"),
    "SA": ("riyadh", "jeddah", "dammam", "khobar", "al khobar", "saudi",
           "ksa", "mecca", "makkah", "medina", "tabuk", "hofuf", "jubail"),
    "QA": ("doha", "qatar", "lusail"),
    "BH": ("manama", "bahrain"),
    "KW": ("kuwait",),
    "OM": ("muscat", "oman"),
}
COUNTRY_TLDS = (
    (".ae", "AE"),
    (".sa", "SA"),
    (".qa", "QA"),
    (".bh", "BH"),
    (".kw", "KW"),
    (".om", "OM"),
)

VERTICAL_KEYWORDS = {
    "clinic": (
        "dental", "dentist", "clinic", "polyclinic", "medical", "optical",
        "optician", "dermatology", "ivf", "diagnostic", "lab", "pharmacy",
        "physiotherapy", "physio", "veterinary", "weight loss",
        "hair transplant", "laser", "skin", "cosmetic",
    ),
    "fitness": ("gym", "fitness", "yoga", "pilates", "crossfit", "ladies"),
    "fnb": (
        "restaurant", "cafe", "café", "coffee", "kitchen", "bakery",
        "burger", "pizza", "shawarma", "grill", "bistro", "lounge",
        "dining", "juice",
    ),
    "retail": ("salon", "spa", "barber", "beauty", "boutique", "store", "shop"),
}

ROLE_LOCALS = {
    "info", "contact", "hello", "hi", "sales", "admin", "support",
    "noreply", "no-reply", "marketing", "hr", "jobs", "careers",
    "enquiry", "enquiries", "inquiry", "inquiries", "booking",
    "appointment", "reception", "service", "customercare",
    "customerservice", "office", "help", "team", "web", "webmaster",
    "operations", "ops", "frontdesk",
}
JUNK_DOMAINS = {
    "example.com", "example.org", "yourdomain.com", "domain.com", "test.com",
    "email.com", "mail.com", "wpforms.com", "sentry.io", "wix.com",
    "wixsite.com", "wordpress.com", "shopify.com",
}
FREEMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}
DIRECTORY_HOST_HINTS = (
    "yellowpages", "yelp.", "tripadvisor", "timeout", "sothebysrealty",
    "kenresearch", "marketresearch", "researchandmarkets", "wikipedia",
)
DIRECTORY_TEXT_HINTS = (
    "top 10", "best ", "list of", "industry report", "market report",
    "market size", "directory", "guide to", "near me",
)


@dataclass(frozen=True)
class FunnelConfig:
    threshold_qualify: int = 70
    rescue_floor: int = 60
    role_inbox_min_score: int = 85
    send_cap: int = 30
    source_multiplier: int = 3
    max_sourcing_passes: int = 3
    require_website: bool = True
    target_countries: tuple[str, ...] = TARGET_COUNTRIES
    target_verticals: tuple[str, ...] = TARGET_VERTICALS


@dataclass(frozen=True)
class FitResult:
    keep: bool
    reason: str


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60] or "lead"


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def domain_of(url: Optional[str]) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def root_domain(host_or_url: str) -> str:
    host = domain_of(host_or_url) or (host_or_url or "").lower()
    parts = [p for p in host.split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def email_root_domain(email: str) -> str:
    return root_domain(email.split("@", 1)[1]) if "@" in email else ""


def detect_country(lead: dict) -> Optional[str]:
    raw = lead.get("raw") or {}
    address_obj = raw.get("address_obj") or {}
    if isinstance(address_obj, dict):
        code = address_obj.get("country")
        if isinstance(code, str) and len(code) == 2:
            return code.upper()
    code = raw.get("country")
    if isinstance(code, str) and len(code) == 2:
        return code.upper()

    text = " ".join([
        str(lead.get("location") or ""),
        str(lead.get("address") or ""),
        str(lead.get("name") or ""),
    ]).lower()
    for country, hints in COUNTRY_HINTS.items():
        if any(hint in text for hint in hints):
            return country

    host = domain_of(lead.get("url") or lead.get("website"))
    for tld, country in COUNTRY_TLDS:
        if host.endswith(tld):
            return country
    return None


def detect_vertical(lead: dict) -> Optional[str]:
    raw = lead.get("raw") or {}
    categories = raw.get("categories") or lead.get("categories") or []
    if isinstance(categories, str):
        categories_text = categories
    else:
        categories_text = " ".join(str(item) for item in categories)
    text = " ".join([
        str(lead.get("name") or lead.get("title") or ""),
        str(raw.get("category") or lead.get("category") or ""),
        categories_text,
    ]).lower()
    for vertical, keywords in VERTICAL_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return vertical
    return None


def is_directory_or_article(lead: dict) -> bool:
    url = lead.get("url") or lead.get("website") or ""
    host = domain_of(url)
    text = " ".join([
        str(lead.get("name") or lead.get("title") or ""),
        str((lead.get("raw") or {}).get("page_title") or ""),
        str((lead.get("raw") or {}).get("discovered_via") or ""),
        url,
    ]).lower()
    return any(hint in host for hint in DIRECTORY_HOST_HINTS) or any(
        hint in text for hint in DIRECTORY_TEXT_HINTS
    )


def email_class(email: str) -> str:
    email = (email or "").strip().lower()
    if not email or "@" not in email or email.count("@") > 1:
        return "missing"
    if "," in email or " " in email or email.startswith("%"):
        return "junk"
    local, domain = email.split("@", 1)
    if len(local) <= 2:
        return "junk"
    if domain in JUNK_DOMAINS or "_" in domain:
        return "junk"
    if re.search(r"\.(png|jpg|jpeg|gif|svg|webp|ico|css|js|html?)$", email):
        return "junk"
    if any(part in {"gov", "mil", "edu"} for part in domain.split(".")):
        return "junk"
    if local in ROLE_LOCALS:
        return "role"
    if domain in FREEMAIL_DOMAINS:
        return "gmail-personal"
    return "person"


def email_matches_domain(email: str, url: Optional[str]) -> bool:
    if not email or "@" not in email or not url:
        return False
    return email_root_domain(email) == root_domain(url)


def best_email(lead: dict) -> tuple[str, str]:
    raw = lead.get("raw") or {}
    candidates = []
    for value in [lead.get("dm_email"), lead.get("email")]:
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip().lower())
    for value in raw.get("all_emails") or []:
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip().lower())

    url = lead.get("url") or lead.get("website")
    unique = []
    for email in candidates:
        if email not in unique:
            unique.append(email)

    valid = []
    for email in unique:
        klass = email_class(email)
        if klass in {"missing", "junk"}:
            continue
        if klass == "person" and url and not email_matches_domain(email, url):
            continue
        valid.append((email, klass))

    if not valid:
        return "", "missing"

    def rank(item: tuple[str, str]) -> tuple[int, int]:
        email, klass = item
        matched = email_matches_domain(email, url)
        class_rank = {"person": 0, "gmail-personal": 1, "role": 2}.get(klass, 9)
        return (class_rank, 0 if matched else 1)

    valid.sort(key=rank)
    return valid[0]


def source_fit(lead: dict, config: FunnelConfig) -> FitResult:
    name = (lead.get("name") or lead.get("title") or "").strip()
    if not name:
        return FitResult(False, "missing_name")
    if "permanently closed" in name.lower() or "temporarily closed" in name.lower():
        return FitResult(False, "closed")
    if is_directory_or_article(lead):
        return FitResult(False, "directory_or_article")

    country = detect_country(lead)
    if country not in config.target_countries:
        return FitResult(False, "outside_target_geo")

    vertical = detect_vertical(lead)
    if vertical not in config.target_verticals:
        return FitResult(False, "outside_target_vertical")

    website = lead.get("website") or lead.get("url")
    if config.require_website and not (website and str(website).startswith("http")):
        return FitResult(False, "no_website")
    if not (lead.get("email") or lead.get("phone") or website):
        return FitResult(False, "no_contact")
    return FitResult(True, "kept")


def source_priority(lead: dict) -> int:
    raw = lead.get("raw") or {}
    review_count = int(raw.get("review_count") or lead.get("review_count") or 0)
    rating = float(raw.get("rating") or lead.get("rating") or 0.0)
    email, klass = best_email(lead)
    website = lead.get("website") or lead.get("url")

    priority = 0
    if detect_country(lead) in ("AE", "SA"):
        priority += 8
    elif detect_country(lead) in ("QA", "BH", "KW"):
        priority += 5
    if detect_vertical(lead) in ("clinic", "fitness", "fnb", "retail"):
        priority += 8
    if website and str(website).startswith("http"):
        priority += 4
    if klass == "person" and email_matches_domain(email, website):
        priority += 10
    elif klass in {"person", "gmail-personal"}:
        priority += 6
    elif klass == "role":
        priority += 1
    priority += min(review_count // 25, 30)
    if rating >= 4.4 and review_count >= 50:
        priority += 3
    return priority


def select_source_batch(leads: list[dict], config: FunnelConfig) -> list[dict]:
    """Limit pre-score work to the markdown route's focused source batch size."""
    limit = max(config.send_cap * config.source_multiplier, config.send_cap)
    ranked = sorted(leads, key=source_priority, reverse=True)
    return ranked[:limit]


def select_source_workset(leads: list[dict], config: FunnelConfig) -> list[dict]:
    """Model the orchestrator's bounded sourcing loop over existing artifacts."""
    batch_limit = max(config.send_cap * config.source_multiplier, config.send_cap)
    total_limit = batch_limit * max(config.max_sourcing_passes, 1)
    ranked = sorted(leads, key=source_priority, reverse=True)
    return ranked[:total_limit]


def score_lead(lead: dict, config: FunnelConfig) -> dict:
    country = detect_country(lead)
    vertical = detect_vertical(lead)
    chosen_email, klass = best_email(lead)
    raw = lead.get("raw") or {}
    review_count = int(raw.get("review_count") or lead.get("review_count") or 0)
    rating = float(raw.get("rating") or lead.get("rating") or 0.0)
    website = lead.get("website") or lead.get("url") or ""
    has_website = bool(website and str(website).startswith("http"))
    has_phone = bool(lead.get("phone") or raw.get("phone"))

    score = 0
    breakdown = {"fit": 0, "size": 0, "reach": 0, "gap": 0, "buyer": 0, "bonus": 0}
    matched = []

    if vertical in config.target_verticals:
        score += 12
        breakdown["fit"] += 12
        matched.append(f"fit:+12: target vertical {vertical}")
    if country in ("AE", "SA"):
        score += 8
        breakdown["fit"] += 8
        matched.append(f"fit:+8: priority country {country}")
    elif country in ("QA", "BH", "KW"):
        score += 5
        breakdown["fit"] += 5
        matched.append(f"fit:+5: target country {country}")

    size = 0
    if review_count >= 200:
        size += 15
    elif review_count >= 100:
        size += 10
    elif review_count >= 50:
        size += 6
    elif review_count >= 10:
        size += 3
    if has_website:
        size += 4
    size = min(size, 20)
    score += size
    breakdown["size"] = size
    if size:
        matched.append(f"size:+{size}: review/website footprint")

    reach = 0
    if klass == "person":
        reach = 18 if email_matches_domain(chosen_email, website) else 12
    elif klass == "gmail-personal":
        reach = 10
    elif klass == "role":
        reach = 2
    score += reach
    breakdown["reach"] = reach
    if reach:
        matched.append(f"reach:+{reach}: email_class={klass}")

    gap = 0
    if vertical in ("clinic", "fitness", "fnb"):
        gap += 6
    elif vertical == "retail":
        gap += 4
    if has_phone:
        gap += 3
    if review_count >= 200:
        gap += 5
    if rating >= 4.4 and review_count >= 200:
        gap += 2
    gap = min(gap, 16)
    score += gap
    breakdown["gap"] = gap
    if gap:
        matched.append(f"gap:+{gap}: pre-signal manual-ops likelihood")

    buyer = 0
    if klass == "person":
        buyer = 6
    elif klass == "gmail-personal":
        buyer = 4
    score += buyer
    breakdown["buyer"] = buyer
    if buyer:
        matched.append(f"buyer:+{buyer}: buyer-reachable email")

    if rating >= 4.4 and review_count >= 500:
        score += 5
        breakdown["bonus"] = 5
        matched.append("bonus:+5: high-volume reputation")

    score = max(0, min(100, score))
    status = "drop"
    disqualified_by = None
    if klass in {"missing", "junk"}:
        disqualified_by = f"email_class={klass}"
    elif klass == "role" and score < config.role_inbox_min_score:
        disqualified_by = "role_inbox_below_85"
    elif score >= config.threshold_qualify:
        status = "send_ready"
    elif score >= config.rescue_floor and klass in {"person", "gmail-personal"}:
        status = "signal_rescue"
    else:
        disqualified_by = "score_below_rescue_floor"

    return {
        **lead,
        "lead_id": lead.get("lead_id") or lead.get("id") or slugify(lead.get("name") or ""),
        "lead_slug": lead.get("lead_slug") or slugify(lead.get("name") or ""),
        "website": website,
        "email": chosen_email,
        "email_class": klass,
        "country": country,
        "vertical": vertical,
        "score": score,
        "score_breakdown": breakdown,
        "matched_criteria": matched,
        "primary_gap": lead.get("primary_gap") or "manual_intake",
        "qualification_status": status,
        "send_eligible": status == "send_ready",
        "disqualified_by": disqualified_by,
    }


def has_real_signal(signals: dict) -> bool:
    return any(key in (signals or {}) for key in REAL_SIGNAL_KEYS)


def primary_signal(signals: dict) -> str:
    for key in REAL_SIGNAL_KEYS:
        if key in (signals or {}):
            return key
    return "fallback"


def load_sent_emails(path: Optional[Path]) -> set[str]:
    if not path or not path.exists():
        return set()
    text = path.read_text(encoding="utf-8", errors="ignore")
    return {m.lower() for m in re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)}


def build_signal_pool(scored: list[dict], config: FunnelConfig, sent_emails: set[str]) -> list[dict]:
    pool = []
    seen_domains = set()
    for lead in sorted(scored, key=lambda row: -(row.get("score") or 0)):
        if lead.get("qualification_status") not in {"send_ready", "signal_rescue"}:
            continue
        email = (lead.get("email") or "").lower()
        if not email or email in sent_emails:
            continue
        website = lead.get("website") or lead.get("url")
        if config.require_website and not website:
            continue
        email_domain = email.split("@", 1)[1] if "@" in email else ""
        business_domain = root_domain(website or "")
        if business_domain:
            dedupe_key = f"site:{business_domain}"
        elif email_domain and email_domain not in FREEMAIL_DOMAINS:
            dedupe_key = f"email_domain:{email_domain}"
        else:
            dedupe_key = f"email:{email}"
        if dedupe_key in seen_domains:
            continue
        seen_domains.add(dedupe_key)
        pool.append(lead)
    return pool


def signal_index(signal_rows: list[dict]) -> dict[str, dict]:
    indexed = {}
    for row in signal_rows:
        signals = row.get("signals") or row
        for key in (row.get("lead_id"), row.get("id"), row.get("website"), row.get("url"), row.get("name")):
            if key:
                indexed[str(key).lower()] = signals
    return indexed


def apply_existing_signals(pool: list[dict], signals: list[dict]) -> list[dict]:
    indexed = signal_index(signals)
    out = []
    for lead in pool:
        keys = [
            lead.get("lead_id"),
            lead.get("id"),
            lead.get("website"),
            lead.get("url"),
            lead.get("name"),
        ]
        sig = {}
        for key in keys:
            if key and str(key).lower() in indexed:
                sig = indexed[str(key).lower()]
                break
        if has_real_signal(sig):
            updated = {
                **lead,
                "concrete_signal": True,
                "signal_used": primary_signal(sig),
                "signals": sig,
                "send_eligible": True,
            }
            out.append(updated)
        else:
            out.append({
                **lead,
                "concrete_signal": False,
                "send_eligible": False,
            })
    return out


def parse_int_field(text: str, name: str, default: int) -> int:
    match = re.search(rf"\b{name}\s*:\s*(\d+)", text)
    return int(match.group(1)) if match else default


def config_from_icp(path: Path, target_override: Optional[int] = None) -> FunnelConfig:
    if not path.exists():
        return FunnelConfig(send_cap=target_override or FunnelConfig.send_cap)
    text = path.read_text(encoding="utf-8", errors="ignore")
    threshold = parse_int_field(text, "threshold_qualify", 70)
    hard_floor = parse_int_field(text, "hard_floor", 60)
    send_cap = target_override or parse_int_field(text, "send_cap", int(os.environ.get("MAX_EMAILS_PER_RUN", "30")))
    return FunnelConfig(
        threshold_qualify=threshold,
        rescue_floor=hard_floor,
        send_cap=send_cap,
    )


def run_dry(run_dir: Path, config: FunnelConfig, sent_log: Optional[Path]) -> dict:
    deduped = run_dir / "leads-deduped.json"
    if not deduped.exists():
        raise SystemExit(f"missing input: {deduped}")

    leads = load_jsonl(deduped)
    fit_rows = []
    source_drops: dict[str, int] = {}
    for lead in leads:
        fit = source_fit(lead, config)
        if fit.keep:
            fit_rows.append(lead)
        else:
            source_drops[fit.reason] = source_drops.get(fit.reason, 0) + 1

    selected = select_source_workset(fit_rows, config)
    scored = [score_lead(lead, config) for lead in selected]
    sent = load_sent_emails(sent_log)
    pool = build_signal_pool(scored, config, sent)

    signal_file = run_dir / "lead-signals.json"
    signal_rows = load_jsonl(signal_file)
    signal_checked = apply_existing_signals(pool, signal_rows) if signal_rows else pool
    send_ready = [row for row in signal_checked if row.get("send_eligible")]

    write_jsonl(run_dir / "leads-source-fit.json", fit_rows)
    write_jsonl(run_dir / "leads-scored-balanced.json", scored)
    write_jsonl(run_dir / "leads-signal-pool.json", pool)
    write_jsonl(run_dir / "leads-send-eligible.json", send_ready[:config.send_cap])

    status_counts: dict[str, int] = {}
    for row in scored:
        key = row.get("qualification_status") or "unknown"
        status_counts[key] = status_counts.get(key, 0) + 1

    report = {
        "input_leads": len(leads),
        "source_fit_kept": len(fit_rows),
        "source_batch_limit": config.send_cap * config.source_multiplier,
        "max_sourcing_passes": config.max_sourcing_passes,
        "source_workset_limit": config.send_cap * config.source_multiplier * config.max_sourcing_passes,
        "source_batch_selected": len(selected),
        "source_fit_dropped": source_drops,
        "scored": len(scored),
        "qualification_status": status_counts,
        "signal_pool": len(pool),
        "signal_rows_available": len(signal_rows),
        "send_eligible_with_existing_signals": len(send_ready),
        "send_cap": config.send_cap,
        "role_inbox_min_score": config.role_inbox_min_score,
        "outputs": {
            "source_fit": str(run_dir / "leads-source-fit.json"),
            "scored": str(run_dir / "leads-scored-balanced.json"),
            "signal_pool": str(run_dir / "leads-signal-pool.json"),
            "send_eligible": str(run_dir / "leads-send-eligible.json"),
        },
    }
    (run_dir / "balanced-funnel-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--target", type=int, default=None)
    parser.add_argument("--sent-log", default=None)
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    config = config_from_icp(run_dir / "icp.yaml", args.target)
    sent_log = Path(args.sent_log) if args.sent_log else None
    report = run_dry(run_dir, config, sent_log)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

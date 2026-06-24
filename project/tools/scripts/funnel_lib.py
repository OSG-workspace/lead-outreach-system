#!/usr/bin/env python3
"""Reusable deterministic funnel helpers for the outreach pipeline.

The markdown pipeline is the contract: source-fit before scoring, balanced
scoring before signal qualification, and hard send gates before Brevo.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse


COUNTRY_ALIASES = {
    "united arab emirates": "AE",
    "uae": "AE",
    "ae": "AE",
    "saudi arabia": "SA",
    "ksa": "SA",
    "sa": "SA",
    "qatar": "QA",
    "qa": "QA",
    "bahrain": "BH",
    "bh": "BH",
    "kuwait": "KW",
    "kw": "KW",
    "oman": "OM",
    "om": "OM",
}
COUNTRY_HINTS = {
    "AE": ("dubai", "abu dhabi", "sharjah", "ajman", "ras al khaimah", "fujairah", "uae", "united arab", "al ain"),
    "SA": ("riyadh", "jeddah", "dammam", "khobar", "saudi", "ksa", "makkah", "mecca", "medina", "jubail"),
    "QA": ("doha", "qatar", "lusail"),
    "BH": ("manama", "bahrain"),
    "KW": ("kuwait",),
    "OM": ("muscat", "oman"),
}
COUNTRY_TLDS = {".ae": "AE", ".sa": "SA", ".qa": "QA", ".bh": "BH", ".kw": "KW", ".om": "OM"}
FREEMAIL_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "protonmail.com"}
BLOCKED_WEBSITE_HOSTS = {
    "instagram.com",
    "facebook.com",
    "m.facebook.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "youtube.com",
    "wa.me",
    "whatsapp.com",
    "api.whatsapp.com",
    "forms.gle",
    "docs.google.com",
    "g.page",
    "linktr.ee",
    "beacons.ai",
    "bio.link",
    "linkstn.com",
    "qr1.me-qr.com",
    "lliink.com",
}
DIRECTORY_OR_MEDIA_HOST_HINTS = (
    "edarabia.com",
    "gqmiddleeast.com",
    "filipinotimes.net",
    "araboo.com",
    "arabiantalks.com",
    "useallot.com",
    "dubai-online.com",
    "visionplusmag.com",
    "dxbproperties.ae",
    "arabiamd.com",
    "bizpreneurme.com",
    "kenresearch.com",
    "sothebysrealty.ae",
    "mandarinoriental.com",
    "banyantree.com",
    "morecravings.com",
    "yango.com",
    "google.com",
)
VERTICAL_KEYWORDS = {
    "clinic": ("clinic", "dental", "medical", "dermatology", "ivf", "diagnostic", "optical", "pharmacy", "hospital", "polyclinic"),
    "fitness": ("gym", "fitness", "yoga", "pilates", "crossfit", "studio"),
    "fnb": ("restaurant", "cafe", "coffee", "kitchen", "bakery", "burger", "pizza", "grill", "bistro", "cloud kitchen"),
    "retail": ("retail", "apparel", "beauty", "salon", "spa", "boutique", "home", "store"),
}
ROLE_LOCAL_RE = re.compile(
    r"^("
    r"info|contact|contactus|hello|hi|sales|admin|support|marketing|"
    r"hr|jobs|careers|recruit|recruitment|recruiting|"
    r"booking|bookings|appointment|appointments|reservations|reserve|"
    r"reception|office|team|enquiry|enquiries|inquiry|inquiries|"
    r"mail|email|webmaster|postmaster|noreply|no\-?reply|reply|donotreply|"
    r"customercare|customerservice|customer|care|client|clients|clientcare|clientservice|"
    r"service|services|servicedesk|help|helpdesk|helpline|"
    r"press|media|comms|communications|"
    r"finance|accounts|accounting|billing|invoice|invoices|orders|order|payment|payments|"
    r"legal|compliance|complaint|complaints|whistleblowing|whistleblower|"
    r"setup|setups|onboarding|"
    r"leads|reachus|reach|getintouch|talk|talktous|"
    r"general|public|main|primary|"
    r"guides|guide|tips|news|newsletter|subscribe|subscription|"
    r"manager|director|chief|head|principal|"
    r"motor|home|life|health|car|auto|"
    r"online|onlinestore|store|shop|webshop|ecom|ecommerce|"
    r"branch|branches|department|departments|dept|division|"
    r"ask|askalfred|alfred|bot|chat|assistant|"
    r"contact[a-z]{0,6}|info[a-z\.\-]{0,8}"
    r")@",
    re.IGNORECASE,
)
ROLE_LOCAL_CONTAINS_RE = re.compile(
    r"^(?=[^@]{0,40}@)(?=[^@]*("
    r"office|customer|reception|booking|enquir|inquir|"
    r"info|sales|admin|support|marketing|finance|accounts|"
    r"legal|compliance|press|media|recruit|jobs|careers|"
    r"complaint|service|care|reservations|onboarding|"
    r"general|public|info\."
    r"))[^@]*@",
    re.IGNORECASE,
)
ROLE_LOCAL_GEO_RE = re.compile(
    r"^("
    r"ksa|uae|qa|qatar|bahrain|kuwait|oman|saudi|"
    r"dubai|abudhabi|sharjah|riyadh|jeddah|doha|manama|kuwaitcity|muscat|"
    r"[a-z]{2,12}office|[a-z]{2,12}branch|[a-z]{2,12}team|[a-z]{2,12}hq|"
    r"(info|contact|sales|hello|admin|support|marketing|service|services|care|booking|enquiry|inquiry|orders|office|reservations)\.[a-z0-9]{2,15}|"
    r"(gi|gen|geninfo)\.[a-z]{2,15}"
    r")@",
    re.IGNORECASE,
)
JUNK_EMAIL_RE = re.compile(
    r"\.(png|jpg|jpeg|gif|svg|webp|ico|css|js|woff|html?)$|"
    r"@(example\.com|example\.org|yourdomain\.com|domain\.com|test\.com|email\.com|mail\.com)$|"
    r"@.*\.(gov|mil|edu)$|\.(gov|mil|edu)\.",
    re.IGNORECASE,
)
REAL_SIGNAL_KEYS = ("mode", "owner_name", "ai_mentioned", "saas", "branches", "hiring_manual", "pdf_menu")
STRONG_SIGNAL_KEYS = ("mode", "hiring_manual", "branches", "ai_mentioned", "saas", "pdf_menu")


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    if not path.exists():
        return rows
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def domain_of(url: str | None) -> str:
    if not url:
        return ""
    if not re.match(r"^https?://", url):
        url = "https://" + url.lstrip("/")
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def root_domain(host_or_url: str | None) -> str:
    host = domain_of(host_or_url) if (host_or_url or "").startswith(("http://", "https://")) else (host_or_url or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = [part for part in host.split(".") if part]
    if len(parts) < 2:
        return host
    if len(parts) >= 3 and parts[-2] in {"com", "net", "org", "gov", "edu", "ac", "co"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def lead_url(lead: dict) -> str:
    return lead.get("url") or lead.get("website") or ""


def slugify(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:80] or "lead"


def _extract_scalar(text: str, key: str, default: int) -> int:
    m = re.search(rf"^\s*{re.escape(key)}:\s*(\d+)\b", text, flags=re.MULTILINE)
    return int(m.group(1)) if m else default


def _extract_countries(text: str) -> list[str]:
    block = ""
    m = re.search(r"countries:\s*\n(?P<body>(?:\s+- .+\n)+)", text)
    if m:
        block = m.group("body")
    countries: list[str] = []
    for item in re.findall(r"-\s*[\"']?([^\"'\n]+)", block):
        code = normalize_country(item)
        if code and code not in countries:
            countries.append(code)
    return countries or ["AE", "SA", "QA", "BH", "KW"]


_INDUSTRY_STOPWORDS = {
    "the", "and", "or", "of", "in", "on", "for", "with", "a", "an", "to",
    "groups", "group", "chains", "chain", "businesses", "business",
    "company", "companies", "firm", "firms", "services", "service",
    "providers", "provider", "agency", "agencies", "industry", "sector",
    "based", "small", "medium", "mid", "market", "led", "only", "etc",
}


def _industry_keywords_from_phrase(phrase: str) -> list[str]:
    """Pull keyword tokens out of an industry description.

    "clinic groups (dental, optical, medical)" ->
        ["clinic", "dental", "optical", "medical"]
    "real estate agencies in UAE / KSA" ->
        ["real estate", "real", "estate"]
    """
    if not phrase:
        return []
    cleaned = phrase.lower()
    # Strip parenthetical commentary but keep the words inside as keywords.
    cleaned = re.sub(r"[()]+", ",", cleaned)
    # Split on punctuation/slashes/commas.
    parts = re.split(r"[,/;|]+", cleaned)
    keywords: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Take the multi-word phrase itself (e.g., "real estate") and
        # also individual content words.
        if " " in part:
            kw = part.strip()
            if kw and kw not in keywords:
                keywords.append(kw)
        for token in part.split():
            token = re.sub(r"[^a-z0-9]", "", token)
            if len(token) < 3 or token in _INDUSTRY_STOPWORDS:
                continue
            if token not in keywords:
                keywords.append(token)
    return keywords


def _extract_industry_keywords(text: str) -> list[str]:
    """Pull keywords out of every line under `target.industries:` in the ICP YAML."""
    m = re.search(
        r"^(?:  )?industries:\s*\n(?P<body>(?:\s{2,}-\s+.+\n?)+)",
        text,
        flags=re.MULTILINE,
    )
    if not m:
        return []
    keywords: list[str] = []
    for line in m.group("body").splitlines():
        m2 = re.match(r"\s*-\s+[\"']?(?P<v>[^\"'\n]+)[\"']?\s*$", line)
        if not m2:
            continue
        for kw in _industry_keywords_from_phrase(m2.group("v")):
            if kw not in keywords:
                keywords.append(kw)
    return keywords


def _extract_verticals(text: str) -> list[str]:
    """Vertical labels recognized in this run.

    First tries the ICP's `industries:` block; falls back to the hardcoded
    consumer-chain taxonomy only if the ICP has no industries block.
    """
    industry_keywords = _extract_industry_keywords(text)
    if industry_keywords:
        # Each industry phrase becomes its own vertical "label", PLUS any of
        # the legacy labels its keywords overlap with (so existing
        # consumer-chain scoring still triggers on dental/gym/etc.).
        labels: list[str] = []
        for vertical, kws in VERTICAL_KEYWORDS.items():
            if any(any(kw == ik or kw in ik or ik in kw for kw in kws)
                   for ik in industry_keywords):
                labels.append(vertical)
        # Every distinct industry keyword can also act as a vertical label.
        for kw in industry_keywords:
            if kw not in labels:
                labels.append(kw)
        return labels
    found: list[str] = []
    lower = text.lower()
    for vertical, keywords in VERTICAL_KEYWORDS.items():
        if any(keyword in lower for keyword in keywords) and vertical not in found:
            found.append(vertical)
    return found or ["clinic", "fitness", "fnb", "retail"]


def _extract_branch_range(text: str) -> tuple[int, int]:
    m = re.search(r"(\d+)\s*-\s*(\d+)\s+(?:locations|branches)", text, flags=re.IGNORECASE)
    if m:
        return int(m.group(1)), int(m.group(2))
    return 1, 999


def build_funnel_config(run_dir: Path) -> dict:
    text = (run_dir / "icp.yaml").read_text()
    target = _extract_scalar(text, "send_cap", _extract_scalar(text, "qualified_target", 30))
    qualified_target = _extract_scalar(text, "qualified_target", target)
    min_branches, max_branches = _extract_branch_range(text)
    role_threshold = 85
    generic_match = re.search(
        r"generic_inbox_policy:[\s\S]{0,400}?(?:score\s*>=\s*|score\s+)(\d+)",
        text,
        re.IGNORECASE,
    )
    if generic_match:
        role_threshold = int(generic_match.group(1))
    return {
        "run_dir": str(run_dir),
        "target_sends": target,
        "qualified_target": qualified_target,
        "source_multiplier": 3,
        "source_target": target * 3,
        "max_sourcing_passes": 3,
        "threshold_qualify": _extract_scalar(text, "threshold_qualify", 70),
        "threshold_send": _extract_scalar(text, "threshold_send", _extract_scalar(text, "threshold_qualify", 70)),
        "rescue_min_score": _extract_scalar(text, "hard_floor", 60),
        "role_inbox_threshold": role_threshold,
        "target_countries": _extract_countries(text),
        "target_verticals": _extract_verticals(text),
        "target_vertical_keywords": _extract_industry_keywords(text),
        "min_branches": min_branches,
        "max_branches": max_branches,
        "require_website": "must have a public website" in text.lower(),
        "require_contact": True,
    }


def normalize_country(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().strip('"').strip("'").lower()
    return COUNTRY_ALIASES.get(cleaned)


def detect_country(lead: dict) -> str | None:
    raw = lead.get("raw") or {}
    for key in ("country",):
        code = normalize_country(str(raw.get(key) or ""))
        if code:
            return code
    address_obj = raw.get("address_obj")
    if isinstance(address_obj, dict):
        code = normalize_country(str(address_obj.get("country") or ""))
        if code:
            return code
    text = " ".join(str(x or "") for x in (lead.get("location"), lead.get("address"), lead.get("name"), raw.get("category"))).lower()
    for code, hints in COUNTRY_HINTS.items():
        if any(hint in text for hint in hints):
            return code
    host = domain_of(lead_url(lead))
    for tld, code in COUNTRY_TLDS.items():
        if host.endswith(tld):
            return code
    return None


def detect_vertical(lead: dict, cfg: dict | None = None) -> str | None:
    raw = lead.get("raw") or {}
    cats = raw.get("category") or " ".join(raw.get("categories") or [])
    text = f"{lead.get('name') or ''} {cats}".lower()
    # If the ICP supplied its own industry keywords, those are authoritative.
    # Do NOT silently fall back to the hardcoded consumer-chain taxonomy in
    # that case — the brief said B2B, we honor B2B.
    keywords = (cfg or {}).get("target_vertical_keywords") or []
    if keywords:
        for kw in keywords:
            if kw and kw in text:
                for label, kws in VERTICAL_KEYWORDS.items():
                    if kw in kws:
                        return label
                return kw
        return None
    for vertical, kws in VERTICAL_KEYWORDS.items():
        if any(keyword in text for keyword in kws):
            return vertical
    return lead.get("vertical")


def detect_branches(lead: dict) -> int | None:
    for key in ("branches", "branch_count", "locations", "location_count"):
        try:
            value = lead.get(key)
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            pass
    raw = lead.get("raw") or {}
    related = raw.get("related_places") or raw.get("branches")
    if isinstance(related, list) and related:
        return len(related)
    text = " ".join(str(x or "") for x in (lead.get("name"), lead.get("description"), raw.get("category"))).lower()
    m = re.search(r"(\d{1,3})\s+(?:branches|locations|outlets|clinics|stores|gyms|centers|centres)", text)
    return int(m.group(1)) if m else None


def real_website(url: str | None) -> bool:
    host = domain_of(url)
    if not host:
        return False
    root = root_domain(host)
    if root in BLOCKED_WEBSITE_HOSTS or host in BLOCKED_WEBSITE_HOSTS:
        return False
    if any(host == blocked or host.endswith("." + blocked) for blocked in BLOCKED_WEBSITE_HOSTS):
        return False
    if any(hint in host for hint in DIRECTORY_OR_MEDIA_HOST_HINTS):
        return False
    if ".gov." in host or host.endswith(".gov") or ".gov." in root or root.endswith(".gov"):
        return False
    return True


def _sanitize_email(value: str) -> str:
    """Normalize an email candidate before classification/use.

    Real-world Maps scrapes pick up emails wrapped in mailto: hrefs that often
    contain URL-encoded whitespace (`mailto:%20info@example.com`). Brevo
    rejects these as `email is not valid in to`. Strip the URL-encoding and
    any leading/trailing whitespace/percent-encodings, lowercase, and bail
    out if the result no longer matches a valid email shape.
    """
    if not isinstance(value, str):
        return ""
    s = value.strip().lower()
    # Strip mailto: prefix and any URL-encoded whitespace runs.
    if s.startswith("mailto:"):
        s = s[7:]
    # Replace common URL-encoded chars at boundaries.
    s = re.sub(r"(?:%20|%09|%0a|%0d|\s)+", "", s)
    s = s.strip(".,;:<>()[]\"'")
    if not s or "@" not in s or s.count("@") != 1:
        return ""
    local, _, domain = s.partition("@")
    if not local or not domain or "." not in domain:
        return ""
    return s


def _email_candidates(lead: dict) -> list[str]:
    candidates: list[str] = []
    for key in ("dm_email", "to_email", "email"):
        value = lead.get(key)
        if isinstance(value, str) and value.strip():
            cleaned = _sanitize_email(value)
            if cleaned:
                candidates.append(cleaned)
    signals = lead.get("signals") or {}
    for value in signals.get("emails") or []:
        if isinstance(value, str) and value.strip():
            cleaned = _sanitize_email(value)
            if cleaned:
                candidates.append(cleaned)
    raw = lead.get("raw") or {}
    for value in raw.get("all_emails") or raw.get("enriched_emails") or []:
        if isinstance(value, str) and value.strip():
            cleaned = _sanitize_email(value)
            if cleaned:
                candidates.append(cleaned)
    return list(dict.fromkeys(candidates))


def email_matches_lead_domain(email: str, lead: dict) -> bool:
    if "@" not in (email or ""):
        return False
    _, domain = email.lower().split("@", 1)
    if domain in FREEMAIL_DOMAINS:
        return True
    lead_root = root_domain(lead_url(lead))
    if not lead_root:
        return True
    return root_domain(domain) == lead_root


def best_email(lead: dict) -> str:
    valid = [
        email for email in _email_candidates(lead)
        if email_class(email) != "junk" and email_matches_lead_domain(email, lead)
    ]
    valid.sort(key=lambda email: {"person": 0, "personal": 1, "role": 2, "missing": 3, "junk": 4}[email_class(email)])
    return valid[0] if valid else ""


def email_class(email: str | None) -> str:
    email = (email or "").strip().lower()
    if not email or "@" not in email or email.count("@") != 1 or "," in email or " " in email:
        return "missing"
    local, domain = email.split("@", 1)
    if len(local) <= 2 or JUNK_EMAIL_RE.search(email):
        return "junk"
    if ROLE_LOCAL_RE.match(email) or ROLE_LOCAL_CONTAINS_RE.match(email) or ROLE_LOCAL_GEO_RE.match(email):
        return "role"
    if domain in FREEMAIL_DOMAINS:
        return "personal"
    return "person"


def resolve_email_pool(rows: list[dict]) -> tuple[list[dict], Counter]:
    resolved: list[dict] = []
    drops: Counter = Counter()
    seen: set[str] = set()
    for row in rows:
        lead = dict(row)
        email = best_email(lead)
        if not email:
            drops["no_clean_email"] += 1
            continue
        eclass = email_class(email)
        domain = email.split("@", 1)[1]
        key = email if domain in FREEMAIL_DOMAINS else (root_domain(lead_url(lead)) or root_domain(domain) or email)
        if key in seen:
            drops["duplicate_contact"] += 1
            continue
        seen.add(key)
        lead["email"] = email
        lead["email_class"] = eclass
        if eclass in {"person", "personal"} and not lead.get("dm_email"):
            lead["dm_email"] = email
        resolved.append(lead)
    return resolved, drops


def has_concrete_signal(lead: dict) -> bool:
    signals = lead.get("signals") or {}
    if any(key in signals and signals[key] not in (None, "", False, []) for key in REAL_SIGNAL_KEYS):
        return True
    signal_used = lead.get("signal_used") or lead.get("primary_gap")
    return bool(signal_used and signal_used not in {"fallback", "manual_intake", "unknown", "none"})


def has_strong_signal(lead: dict) -> bool:
    signals = lead.get("signals") or {}
    return any(key in signals and signals[key] not in (None, "", False, []) for key in STRONG_SIGNAL_KEYS) or has_concrete_signal(lead)


def signal_used(lead: dict) -> str:
    signals = lead.get("signals") or {}
    for key in REAL_SIGNAL_KEYS:
        if key in signals and signals[key] not in (None, "", False, []):
            return key
    value = lead.get("signal_used") or lead.get("primary_gap") or ""
    return "" if value in {"fallback", "manual_intake", "unknown", "none"} else value


def normalize_maps_record(record: dict) -> dict:
    address = record.get("complete_address") or record.get("address") or ""
    if isinstance(address, dict):
        address_str = ", ".join(str(v) for v in address.values() if isinstance(v, str) and v.strip())
    else:
        address_str = str(address or "")
    emails = record.get("emails") or []
    if isinstance(emails, str):
        emails = [emails] if emails else []
    title = (record.get("title") or record.get("name") or "").strip()
    website = record.get("web_site") or record.get("website") or record.get("url") or ""
    return {
        "id": record.get("id") or f"maps-{record.get('place_id') or slugify(title)}",
        "name": title,
        "source": "google_maps",
        "url": website or record.get("link"),
        "email": emails[0] if emails else record.get("email"),
        "phone": record.get("phone"),
        "location": address_str,
        "raw": {
            "google_maps_url": record.get("link"),
            "place_id": record.get("place_id"),
            "rating": record.get("review_rating") if record.get("review_rating") is not None else record.get("rating"),
            "review_count": record.get("review_count"),
            "category": record.get("category"),
            "categories": record.get("categories"),
            "all_emails": emails,
            "website": website,
            "country": address.get("country") if isinstance(address, dict) else record.get("country"),
            "address_obj": address if isinstance(address, dict) else None,
            "related_places": record.get("related_places"),
        },
    }


def normalize_web_record(record: dict) -> dict:
    if record.get("raw") and record.get("id"):
        return dict(record)
    return {
        "id": record.get("id") or f"web-{slugify(record.get('name') or record.get('title') or record.get('url'))}",
        "name": record.get("name") or record.get("title"),
        "source": "web",
        "url": record.get("url") or record.get("website"),
        "email": record.get("email"),
        "phone": record.get("phone"),
        "location": record.get("location") or record.get("snippet"),
        "raw": record.get("raw") or record,
    }


def apply_source_fit(rows: list[dict], cfg: dict) -> tuple[list[dict], Counter]:
    kept: list[dict] = []
    drops: Counter = Counter()
    seen: set[str] = set()
    for original in rows:
        lead = dict(original)
        name = lead.get("name") or ""
        if not name.strip():
            drops["no_name"] += 1
            continue
        country = detect_country(lead)
        vertical = detect_vertical(lead, cfg)
        branches = detect_branches(lead)
        email = best_email(lead)
        website_ok = real_website(lead_url(lead))
        if country not in set(cfg.get("target_countries") or []):
            drops["outside_geo"] += 1
            continue
        # If the ICP supplied data-driven industry keywords, the vertical is
        # whatever those matched. Drop only when nothing matched.
        if not vertical:
            drops["outside_vertical"] += 1
            continue
        if cfg.get("require_website") and not website_ok:
            drops["no_website"] += 1
            continue
        if cfg.get("require_contact") and not (email or lead.get("phone") or website_ok):
            drops["no_contact"] += 1
            continue
        if cfg.get("min_branches", 1) > 1:
            if branches is None:
                lead["branch_fit"] = "unknown"
            elif branches < cfg["min_branches"]:
                drops["below_min_branches"] += 1
                continue
        if branches is not None and branches > cfg.get("max_branches", 999):
            drops["above_max_branches"] += 1
            continue
        host = domain_of(lead_url(lead))
        key = host or slugify(name)
        if key in seen:
            drops["duplicate"] += 1
            continue
        seen.add(key)
        lead.update({"country": country, "vertical": vertical, "branches": branches, "email": email or lead.get("email")})
        kept.append(lead)
    return kept, drops


def score_lead(lead: dict, cfg: dict) -> dict:
    scored = dict(lead)
    country = lead.get("country") or detect_country(lead)
    vertical = lead.get("vertical") or detect_vertical(lead, cfg)
    branches = detect_branches(lead)
    email = best_email(lead)
    eclass = email_class(email)
    raw = lead.get("raw") or {}
    rating = raw.get("rating") or lead.get("rating") or 0
    reviews = raw.get("review_count") or lead.get("review_count") or 0
    matched: list[str] = []
    breakdown = {"fit": 0, "size": 0, "reachability": 0, "gap_signal": 0, "buyer": 0, "bonus": 0}

    def drop(reason: str) -> dict:
        scored.update({
            "score": 0,
            "score_breakdown": breakdown,
            "matched_criteria": matched,
            "qualifies": False,
            "funnel_status": "drop",
            "drop_reason": reason,
            "country": country,
            "vertical": vertical,
            "branches": branches,
            "email": email,
            "email_class": eclass,
        })
        return scored

    if country not in set(cfg.get("target_countries") or []):
        return drop("outside_geo")
    if not vertical:
        return drop("outside_vertical")
    if cfg.get("require_website") and not real_website(lead_url(lead)):
        return drop("no_website")
    if eclass in {"missing", "junk"}:
        return drop(f"email_class_{eclass}")
    if branches is not None and branches < cfg.get("min_branches", 1):
        return drop("below_min_branches")
    if branches is not None and branches > cfg.get("max_branches", 999):
        return drop("above_max_branches")

    if vertical:
        breakdown["fit"] += 15
        matched.append("vertical_fit")
    if country in {"AE", "SA"}:
        breakdown["fit"] += 10
        matched.append("priority_geo")
    elif country in {"QA", "KW", "BH"}:
        breakdown["fit"] += 6
        matched.append("valid_geo")
    breakdown["fit"] = min(25, breakdown["fit"])

    if branches is not None:
        if cfg.get("min_branches", 1) <= branches <= 15:
            breakdown["size"] += 15
            matched.append("branch_sweet_spot")
        elif branches <= cfg.get("max_branches", 999):
            breakdown["size"] += 10
            matched.append("branch_valid_range")
    else:
        if reviews >= 200:
            breakdown["size"] += 10
            matched.append("review_volume_proxy")
        elif reviews >= 50:
            breakdown["size"] += 6
            matched.append("some_volume_proxy")
        elif eclass in {"person", "personal"} and real_website(lead_url(lead)):
            breakdown["size"] += 7
            matched.append("b2b_size_neutral")
    if real_website(lead_url(lead)):
        breakdown["size"] = min(20, breakdown["size"] + 5)
        matched.append("public_website")

    if eclass == "person":
        breakdown["reachability"] += 20
        breakdown["buyer"] += 8
        matched.extend(["direct_email", "buyer_reachable"])
    elif eclass == "personal":
        breakdown["reachability"] += 16
        breakdown["buyer"] += 6
        matched.extend(["personal_email", "buyer_possible"])
    elif eclass == "role":
        breakdown["reachability"] += 12
        breakdown["buyer"] += 4
        matched.append("role_inbox")
        if real_website(lead_url(lead)):
            breakdown["size"] = min(20, breakdown["size"] + 5)
            matched.append("b2b_size_role_neutral")

    if has_concrete_signal(lead):
        breakdown["gap_signal"] += 20
        matched.append("concrete_signal")
    if has_strong_signal(lead):
        breakdown["gap_signal"] = min(25, breakdown["gap_signal"] + 5)
        matched.append("strong_signal")
    if rating and float(rating) >= 4.4 and reviews and int(reviews) >= 200:
        breakdown["bonus"] += 5
        matched.append("proven_volume")

    score = max(0, min(100, sum(breakdown.values())))
    status = "drop"
    reason = ""
    direct_like = eclass in {"person", "personal"}
    base_threshold = cfg.get("threshold_qualify", 70)
    strong_gap = breakdown["gap_signal"] >= 20
    effective_threshold = base_threshold - 5 if strong_gap else base_threshold
    if eclass == "role" and score < cfg.get("role_inbox_threshold", 85):
        reason = "role_inbox_below_threshold"
    elif score >= effective_threshold:
        status = "send_ready" if has_concrete_signal(lead) else "needs_signal"
    elif score >= cfg.get("rescue_min_score", 60) and has_strong_signal(lead) and direct_like:
        status = "signal_rescue"
    elif score >= cfg.get("rescue_min_score", 60):
        status = "needs_signal"
    else:
        reason = "below_rescue_floor"

    scored.update({
        "score": score,
        "score_breakdown": breakdown,
        "matched_criteria": matched,
        "qualifies": status in {"send_ready", "signal_rescue"},
        "funnel_status": status,
        "drop_reason": reason,
        "country": country,
        "vertical": vertical,
        "branches": branches,
        "email": email,
        "email_class": eclass,
        "primary_gap": lead.get("primary_gap") or (lead.get("signal_used") if has_concrete_signal(lead) else None),
    })
    return scored


def merge_signals(scored: list[dict], signal_rows: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    by_website: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for row in signal_rows:
        sig = row.get("signals") or {}
        if not sig:
            continue
        if row.get("id"):
            by_id[str(row["id"])] = sig
        if row.get("website"):
            by_website[domain_of(row.get("website"))] = sig
        if row.get("name"):
            by_name[slugify(row.get("name"))] = sig
    merged: list[dict] = []
    for lead in scored:
        updated = dict(lead)
        sig = by_id.get(str(lead.get("id"))) or by_website.get(domain_of(lead_url(lead))) or by_name.get(slugify(lead.get("name")))
        if sig:
            updated["signals"] = sig
        merged.append(updated)
    return merged


def qualify_ranked_signals(scored: list[dict], target: int, batch_size: int = 20) -> dict:
    ranked = [
        lead for lead in sorted(scored, key=lambda row: -(row.get("score") or 0))
        if lead.get("funnel_status") in {"send_ready", "signal_rescue", "needs_signal"}
    ]
    qualified: list[dict] = []
    drops: Counter = Counter()
    tested = 0
    for idx in range(0, len(ranked), batch_size):
        batch = ranked[idx:idx + batch_size]
        for lead in batch:
            tested += 1
            if not has_concrete_signal(lead):
                drops["no_concrete_signal"] += 1
                continue
            status = lead.get("funnel_status")
            score = lead.get("score") or 0
            eclass = lead.get("email_class") or email_class(best_email(lead))
            if status == "needs_signal":
                bonus = 25 if has_strong_signal(lead) else (20 if has_concrete_signal(lead) else 0)
                post_score = min(100, score + bonus)
                lead["post_signal_score"] = post_score
                if post_score >= 70:
                    status = "send_ready"
                elif post_score >= 60 and (has_strong_signal(lead) or eclass in {"person", "personal"}):
                    status = "signal_rescue"
                else:
                    drops["not_send_band_after_signal"] += 1
                    continue
            qualified.append({**lead, "qualification_status": status, "qualifies": True})
            if len(qualified) >= target:
                return {"qualified": qualified, "tested": tested, "eligible_pool": len(ranked), "drops": dict(drops), "exhausted": False}
    return {"qualified": qualified, "tested": tested, "eligible_pool": len(ranked), "drops": dict(drops), "exhausted": len(qualified) < target}


def validate_send_draft(draft: dict, sent_emails: set[str], domain_counts: dict[str, int], role_threshold: int = 85) -> tuple[bool, str]:
    raw_email = draft.get("to_email") or draft.get("email") or ""
    email = _sanitize_email(raw_email)
    # Backfill the sanitized address so the actual send uses the cleaned form,
    # not the malformed original. This is what fixed the Brevo `%20info@...`
    # rejections after the first batch.
    if email and email != raw_email.strip().lower():
        draft["to_email"] = email
    if email_class(email) in {"missing", "junk"}:
        return False, "bad_email"
    if email in sent_emails:
        return False, "already_sent"
    domain = email.split("@", 1)[1]
    if domain_counts.get(domain, 0) >= 2:
        return False, "domain_cap"
    status = draft.get("qualification_status") or draft.get("funnel_status")
    if status not in {"send_ready", "signal_rescue"}:
        return False, "missing_qualification_status"
    if not (draft.get("signal_used") or draft.get("primary_gap") or has_concrete_signal(draft)):
        return False, "missing_concrete_signal"
    score = int(draft.get("score") or 0)
    if email_class(email) == "role" and score < role_threshold:
        return False, "role_inbox_below_threshold"
    if not draft.get("subject") or not (draft.get("body_text") or draft.get("body_html")):
        return False, "missing_message"
    return True, "send_eligible"


def _short_business_name(name: str | None) -> str:
    value = re.sub(r"\s*[-–—|].*$", "", name or "").strip().split(",")[0].strip()
    return value[:42].rsplit(" ", 1)[0] if len(value) > 42 and " " in value[:42] else (value[:42] or "your team")


def _country_phrase(code: str | None) -> str:
    return {"AE": "the UAE", "SA": "Saudi Arabia", "QA": "Qatar", "BH": "Bahrain", "KW": "Kuwait"}.get((code or "").upper(), "the GCC")


_CONSUMER_VERTICALS = {"clinic", "fitness", "fnb", "retail"}


def _business_noun(vertical: str | None) -> str:
    """Word to use after 'for a' / 'for the' when describing the lead.

    Consumer verticals are usually multi-location chains; B2B services are
    not. Pick the right noun so paragraph 1 doesn't call a law firm a chain.
    """
    v = (vertical or "").strip().lower()
    if v in _CONSUMER_VERTICALS:
        return "chain"
    return "business"


def _signal_phrase(signal: str, lead: dict) -> str:
    business = _short_business_name(lead.get("name"))
    country = _country_phrase(lead.get("country"))
    noun = _business_noun(lead.get("vertical"))
    signals = lead.get("signals") or {}
    mode = signals.get("mode")
    if signal == "mode" and mode == "whatsapp":
        return f"{business}'s public booking path appears to route customers through WhatsApp. For a {noun} in {country}, that usually means intake, follow-up, and handoffs are still sitting with staff."
    if signal == "mode" and mode == "phone":
        return f"{business}'s visible customer path appears to be phone-led. For a {noun} in {country}, that is exactly the kind of repeated intake work a custom AI system can take off the front desk."
    if signal == "mode":
        scale_phrase = "At chain scale" if noun == "chain" else "For a business that size"
        return f"{business}'s site sends customers through a contact form rather than a real-time workflow. {scale_phrase} in {country}, that creates manual back-and-forth after every inquiry."
    if signal == "branches":
        return f"{business} shows a multi-location footprint publicly. Across branches in {country}, reporting, intake, and customer handoffs tend to become manual work before leadership sees the full cost."
    if signal == "ai_mentioned":
        return f"{business}'s public site mentions AI, but the customer operations layer still looks like a place where a shipped system could remove manual work."
    if signal == "saas":
        return f"{business} already shows signs of a SaaS stack. The useful gap is usually the AI layer that turns those tools into end-to-end operational workflows."
    if signal == "hiring_manual":
        return f"{business}'s public hiring signals point to manual coordinator or front-desk work. That is usually the cleanest first workflow for an owned AI system."
    if signal == "pdf_menu":
        return f"{business} still exposes service or menu information through static documents. That often mirrors manual internal handoffs and reporting."
    return f"{business}'s public footprint shows a concrete operations gap that is specific enough to scope into a small custom AI build."


def _html_body(text: str) -> str:
    return "\n".join(f"<p>{part.replace(chr(10), '<br>')}</p>" for part in text.split("\n\n") if part.strip())


_VERTICAL_PHRASE = {
    "clinic": "clinic groups",
    "fitness": "fitness brands",
    "fnb": "F&B groups",
    "retail": "retail businesses",
    "real": "real estate firms",
    "estate": "real estate firms",
    "logistics": "logistics operators",
    "freight": "freight forwarders",
    "transport": "transport operators",
    "legal": "legal practices",
    "law": "law firms",
    "insurance": "insurance brokers",
    "brokerage": "brokerage firms",
    "accounting": "accounting practices",
    "audit": "audit firms",
    "consultancy": "consultancies",
    "consultancies": "consultancies",
    "construction": "construction firms",
    "trading": "trading companies",
    "property": "property firms",
    "brokers": "brokers",
    "broker": "brokers",
    "agencies": "agencies",
    "agency": "agencies",
    "firms": "firms",
    "firm": "firms",
    "developers": "developers",
    "management": "management firms",
    "contracting": "contracting firms",
    "advisory": "advisory firms",
    "corporate": "corporate advisors",
    "financial": "financial services firms",
    "import": "trading companies",
    "export": "trading companies",
    "fleet": "fleet operators",
    "forwarding": "freight forwarders",
    "underwriting": "underwriters",
}


def _vertical_phrase(vertical: str | None) -> str:
    """Produce a grammatically clean noun phrase for paragraph 2.

    The detected vertical may be a single keyword like "real" or "freight"
    (from the data-driven industry keywords). We map common single keywords
    to a readable plural noun phrase; multi-word phrases pass through; and
    anything we don't know gets a neutral generic fallback.
    """
    v = (vertical or "").strip().lower()
    if not v:
        return "service businesses"
    if v in _VERTICAL_PHRASE:
        return _VERTICAL_PHRASE[v]
    if " " in v:
        return v
    return "service businesses in your sector"


def build_outreach_draft(lead: dict, run_slug: str) -> dict:
    email = best_email(lead)
    signal = signal_used(lead)
    business = _short_business_name(lead.get("name"))
    vertical = lead.get("vertical") or "unknown"
    vertical_phrase = _vertical_phrase(vertical)
    status = lead.get("qualification_status") or lead.get("funnel_status")
    subject_hooks = {
        "mode": "AI for the intake workflow",
        "branches": "AI for branch operations",
        "ai_mentioned": "shipping the AI operations layer",
        "saas": "AI on top of your stack",
        "hiring_manual": "AI for coordinator work",
        "pdf_menu": "AI for static workflow gaps",
    }
    subject = f"{business}: {subject_hooks.get(signal, 'AI for manual operations')}"
    p1 = _signal_phrase(signal, lead)
    p2 = (
        "Automate is an AI consulting firm that designs custom AI systems for mid-market companies in the GCC. "
        f"For {vertical_phrase}, we build the intake, reporting, and handoff workflows around the tools already in place. "
        "You own the system, with no monthly platform lock-in. Not a chatbot, but a custom AI tool that quietly takes work off your team."
    )
    p3 = "A fraction of what global consulting firms charge, and we walk if there is no clear ROI in the first 20 minutes."
    p4 = "Worth a brief call this week?"
    body = f"Hello,\n\n{p1}\n\n{p2}\n\n{p3}\n\n{p4}\n\nRegards,\nDavid\nAutomate, automatelb.com"
    return {
        "lead_id": lead.get("lead_id") or lead.get("id") or slugify(lead.get("name")),
        "lead_slug": lead.get("lead_slug") or slugify(lead.get("name")),
        "to_email": email,
        "to_name": lead.get("decision_maker_name") or lead.get("dm_name") or lead.get("name") or email,
        "subject": subject[:200],
        "body_text": body,
        "body_html": _html_body(body),
        "tags": ["cold-outreach", run_slug, vertical, lead.get("country") or "unknown"],
        "score": int(lead.get("post_signal_score") or lead.get("score") or 0),
        "qualification_status": status,
        "funnel_status": lead.get("funnel_status"),
        "signal_used": signal,
        "primary_gap": signal,
        "email_class": lead.get("email_class") or email_class(email),
    }


def stage_report(run_dir: Path, **counts: int | dict) -> dict:
    report = {"run_dir": str(run_dir), **counts}
    (run_dir / "funnel-stage-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def summarize_status(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "by_status": dict(Counter(row.get("funnel_status") or row.get("qualification_status") or "unknown" for row in rows)),
        "by_drop_reason": dict(Counter(row.get("drop_reason") for row in rows if row.get("drop_reason"))),
    }


def gate_drafts(drafts: list[dict], role_threshold: int = 85) -> tuple[list[dict], dict]:
    sent: set[str] = set()
    domains: defaultdict[str, int] = defaultdict(int)
    eligible: list[dict] = []
    drops: Counter = Counter()
    for draft in sorted(drafts, key=lambda row: -(row.get("score") or 0)):
        ok, reason = validate_send_draft(draft, sent, domains, role_threshold)
        if ok:
            email = (draft.get("to_email") or "").lower()
            sent.add(email)
            domains[email.split("@", 1)[1]] += 1
            eligible.append({**draft, "send_gate": "pass"})
        else:
            drops[reason] += 1
    return eligible, {"input": len(drafts), "eligible": len(eligible), "dropped": dict(drops)}

"""Tests for the prose name recovery + SMTP-rescue gate in enrich_contact_person.py

WHY THIS SUITE EXISTS
The SMTP rescue (Stage 5.5, added 2026-08-20) only fires for leads where a
decision-maker's NAME is known but the email is not. name-finder.md asks for
structured first/last-name fields on a `found:false`, but that is a prompt
instruction to a Haiku agent, not a guarantee: measured across the 80 drops of
2026-08-20-eu-hotels, exactly ONE carried the structured fields while 70 named
the person only in the free-text `reason`. So the prose parser is what actually
decides whether the rescue reaches 1 lead or 70, and it must not regress.

Every case below is taken VERBATIM from that run's real drop reasons — these
are the exact strings the parser has to survive in production, including the
two defects found during development (a blanket IGNORECASE flag capturing verbs
as surnames, and "no persONAL email" tripping a "no person" guard).

enrich_contact_person.py parses argv at import time, so sys.argv is set before
the import below.
"""
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

sys.argv = ["enrich_contact_person.py", "--phase", "prep", "--run-dir", str(PROJECT)]
import enrich_contact_person as ecp  # noqa: E402
from enrich_contact_person import (  # noqa: E402
    recover_name_from_reason,
    is_direct_email,
)


# --- names the parser MUST recover (all real 2026-08-20-eu-hotels reasons) ---
@pytest.mark.parametrize("reason,expected", [
    # Form 1: name in parentheses — the common shape.
    ("name found (Michael Oberrauch, General Manager) but no direct personal email",
     ("Michael", "Oberrauch")),
    ("name found (Claudia Prantl, Managing Director) but no direct email",
     ("Claudia", "Prantl")),
    # REGRESSION: "no persONAL email" must not trip the no-person guard.
    ("decision-maker identified (Uwe Schramm, Hotel Director) but no direct personal email",
     ("Uwe", "Schramm")),
    ("name found (Christoph Ursprunger, Geschäftsführer) but no direct personal email found",
     ("Christoph", "Ursprunger")),
    # Middle names collapse to first + last.
    ("Owner identified (Maria Adelheid Scherer, confirmed via hotel imprint)",
     ("Maria", "Scherer")),
    # Lowercase nobiliary particles must not truncate the surname.
    ("decision-maker identified (Paul de Römph, founder/owner of Romex)",
     ("Paul", "Römph")),
    ("owner found (Mag. Luigi von Pasquali) but no direct email",
     ("Luigi", "Pasquali")),
    # Hyphenated surnames survive intact.
    ("name found (Katharina Richter-Wallmann, Owner/Proprietor) but no direct email",
     ("Katharina", "Richter-Wallmann")),
    # Honorific / academic title is stripped, not read as the first name.
    ("name found (Mag. Martin Herbert Stubenböck, 94% owner) but no direct email",
     ("Martin", "Stubenböck")),
    # Generational suffix is not a surname.
    ("name found (Albert Schwaighofer Jr., Geschäftsführer/CEO)",
     ("Albert", "Schwaighofer")),
    # Form 2: name inline after a role word, no parentheses.
    ("Owner Klaus Frühwirth-Stangl identified via FirmenABC.at and business registration",
     ("Klaus", "Frühwirth-Stangl")),
    ("owner Karina Zehentner confirmed via Austrian business register",
     ("Karina", "Zehentner")),
    ("owner identified as Josef Grander (confirmed via Austrian Firmenbuch FN 390964y)",
     ("Josef", "Grander")),
    # A company-shaped capture in form 1 must fall through to form 2's person.
    ("owner Bernhard Knollseisen confirmed via company registry (Knollseisen GmbH)",
     ("Bernhard", "Knollseisen")),
])
def test_recovers_real_names(reason, expected):
    assert recover_name_from_reason(reason) == expected


# --- reasons where NO person was identified: must recover nothing -----------
@pytest.mark.parametrize("reason", [
    "no named decision-maker found in site pages; only generic department mailboxes",
    "no named decision-maker on site (leadership page 404); no direct email",
    "Business operator identified as Familie Schmidhofer, but no individual person's name found",
    "data quality: website URL points to kayak.com instead of the hotel domain",
    "",
    "   ",
])
def test_returns_nothing_when_no_person_named(reason):
    assert recover_name_from_reason(reason) == ("", "")


def test_never_captures_a_verb_as_a_surname():
    """REGRESSION: a blanket re.IGNORECASE disabled the capitalization
    requirement inside the capture group, so 'Klaus Frühwirth-Stangl identified
    via ...' came back with the surname 'via'."""
    verbs = {"via", "from", "confirmed", "identified", "found", "per", "and", "but"}
    for reason in [
        "Owner Klaus Frühwirth-Stangl identified via FirmenABC.at",
        "owner Karina Zehentner confirmed via Austrian business register",
        "Owner Armin Künig confirmed from multiple sources (website, search results)",
        "Owner Gertraud Rudolph-Stöckl confirmed (female, owner since Nov 2000)",
    ]:
        _, last = recover_name_from_reason(reason)
        assert last.lower() not in verbs, f"captured a verb as surname from: {reason}"


def test_recovered_name_still_faces_the_direct_email_gate():
    """The rescue never lowers the email bar: whatever address the SMTP probe
    returns is still judged by is_direct_email(), so a role mailbox that
    happens to match someone's initials is rejected exactly as before."""
    assert not is_direct_email("gm@hotel.example")
    assert not is_direct_email("info@hotel.example")
    assert not is_direct_email("reservations@hotel.example")
    assert not is_direct_email("direktion@hotel.example")
    assert is_direct_email("michael.oberrauch@hotel.example")


# --- catch-all construction from the site's own observed convention ---------
# On a catch-all domain SMTP proves nothing (43% of a measured 14-domain
# sample of real drops), so the fallback reads the company's convention off its
# OWN scraped pages. The load-bearing guard is the last case: no person-format
# address on the site means NO construction, because constructed addresses are
# this pipeline's documented bounce risk.
def _write_page(raw_dir, domain, body):
    raw_dir.mkdir(parents=True, exist_ok=True)
    # harvest_domain ignores pages under 200 chars, hence the padding.
    (raw_dir / f"{domain}__contact.html").write_text(
        f"<html><body>{body}{'.' * 260}</body></html>")


@pytest.fixture
def site(tmp_path, monkeypatch):
    """Point the module at a temp run dir and clear its format caches."""
    monkeypatch.setattr(ecp, "ROOT", tmp_path)
    ecp._site_email_format.cache_clear()
    ecp._site_has_person_format.cache_clear()
    yield tmp_path / "raw_html"
    ecp._site_email_format.cache_clear()
    ecp._site_has_person_format.cache_clear()


@pytest.mark.parametrize("domain,published,first,last,expected", [
    # firstname.lastname convention
    ("acme.test", "sarah.jones@acme.test", "Michael", "Oberrauch",
     "michael.oberrauch@acme.test"),
    # f.lastname convention — a single-char head means an initial, not a name
    ("beta.test", "m.weber@beta.test", "Uwe", "Schramm", "u.schramm@beta.test"),
    # bare-firstname convention
    ("gamma.test", "christine@gamma.test", "Christine", "Loitfelder",
     "christine@gamma.test"),
])
def test_builds_in_the_conventions_observed_on_the_site(site, domain, published,
                                                        first, last, expected):
    _write_page(site, domain, f"Contact: {published} or info@{domain}")
    built, note = ecp.build_from_site_format(first, last, domain)
    assert built == expected
    assert published in note, "the evidence note must cite the address it learned from"


def test_no_construction_without_person_format_evidence(site):
    """THE key guard. A site publishing only role mailboxes gives no evidence
    of any personal convention, so nothing may be constructed for it."""
    _write_page(site, "delta.test", "Only info@delta.test and bookings@delta.test")
    assert ecp.build_from_site_format("Anna", "Muster", "delta.test") == ("", "")


@pytest.mark.parametrize("role_addr", [
    "hotel@epsilon.test",       # the real regression: read as a bare FIRST NAME
    "welcome@epsilon.test",
    "direktion@epsilon.test",   # non-English role words matter — 31-country campaign
    "rezeption@epsilon.test",
    "prenotazioni@epsilon.test",
])
def test_role_mailbox_is_never_mistaken_for_a_name_convention(site, role_addr):
    """REGRESSION (caught in live e2e testing): the format inference filtered
    with a thin local-part set that omitted 'hotel', 'welcome', 'direktion' and
    the rest of the multilingual role vocabulary. `hotel@dollinger.at` then
    looked like a bare-firstname convention and 'proved' a format that does not
    exist — constructing an address on no real evidence, the exact bounce risk
    this path is fenced against. Inference must filter with is_direct_email."""
    _write_page(site, "epsilon.test", f"Contact us at {role_addr}")
    assert ecp._site_email_format("epsilon.test") == ("", "")
    assert ecp.build_from_site_format("Anna", "Muster", "epsilon.test") == ("", "")


def test_no_construction_when_the_name_lacks_what_the_convention_needs(site):
    """A {first}.{last} company plus a first-name-only person cannot be built."""
    _write_page(site, "acme.test", "Contact: sarah.jones@acme.test")
    assert ecp.build_from_site_format("Michael", "", "acme.test") == ("", "")


def test_constructed_address_still_faces_the_direct_email_gate(site):
    """Construction feeds the same gate as everything else — a person whose
    name collides with a role word is still rejected downstream."""
    _write_page(site, "acme.test", "Contact: sarah.jones@acme.test")
    built, _ = ecp.build_from_site_format("Guest", "Services", "acme.test")
    assert built == "guest.services@acme.test"
    assert not is_direct_email(built), "role-word address must not pass the gate"

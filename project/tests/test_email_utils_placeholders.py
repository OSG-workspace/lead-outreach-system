"""Form-placeholder addresses must never survive into a lead record.

Regression context (2026-08-03): auditing qualified-pending.jsonl found 7 lead
records whose to_email was form-placeholder text. Six were `you@`/`your@`/
`example@` at email.com and were already caught. `ihre@email.de` (German "your")
was not: JUNK_RE anchored the literal `email.com`, so the same placeholder domain
under any other TLD walked straight through into a qualified lead.

The risk is real outreach to a non-existent mailbox, which costs sender
reputation, so the net is widened by DOMAIN WORD rather than by TLD. These tests
pin both directions — the placeholders die and real businesses that merely
contain those words survive.
"""
import sys
from pathlib import Path

import pytest

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

from email_utils import JUNK_RE, PLACEHOLDER_LOCALS


@pytest.mark.parametrize("addr", [
    "you@email.com",
    "your@email.com",
    "example@email.com",
    "ihre@email.de",          # the one that got through
    "votre@email.fr",
    "info@example.co.uk",
    "x@yourdomain.net",
    "a@beispiel.de",
    "b@ejemplo.es",
    "c@esempio.it",
    "d@e-mail.de",
])
def test_placeholder_addresses_are_junk(addr):
    assert JUNK_RE.search(addr), f"{addr} should be rejected as a placeholder"


@pytest.mark.parametrize("addr", [
    "ahoy@harbourelectrical.com.au",
    "works@abcgp.com.au",
    "david@automatelb.com",
    "owner@emailmarketing.com.au",     # 'email' as a word inside a real domain
    "info@emails.com.au",
    "hello@mailchimp.com",
    "contact@domainregistry.com.au",   # 'domain' inside a real domain
    "j@example-plumbing.com.au",       # 'example' inside a real domain
])
def test_real_business_addresses_survive(addr):
    assert not JUNK_RE.search(addr), f"{addr} is a real address and must survive"


@pytest.mark.parametrize("local", ["ihre", "votre", "uw", "din", "dein", "tuo", "seu"])
def test_locale_your_words_are_placeholder_locals(local):
    """`your@` is already covered; its translations must be too, since the forms
    they come from are identical."""
    assert local in PLACEHOLDER_LOCALS

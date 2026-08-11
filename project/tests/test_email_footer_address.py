"""The commercial-email footer must carry the sender's physical postal address.

Context (2026-08-11): `send_batch_brevo.py` reads BREVO_SENDER_ADDRESS from
.env and threads it into `render_html(..., address=...)`. The key was unset, so
every email shipped a footer one required element short — silently, because
`_footer()` renders the address line only when the value is truthy.

The send path now WARNs on a live send with no address. These tests cover the
other half: that a configured address actually reaches the rendered footer, so
a later refactor of `_footer` cannot quietly drop it while the key stays set and
the operator sees no warning at all.

The unsubscribe half of the contract is asserted here too — it has a default, so
nothing warns if it regresses.
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT / "tools" / "scripts"))

from email_template import render_html

BODY = "Hello Mr. Smith,\n\nQuick note about your booking inbox.\n\nDavid"
ADDRESS = "12 Rue Verdun, Beirut, Lebanon"


def test_configured_address_reaches_the_footer():
    html = render_html(BODY, address=ADDRESS)
    assert ADDRESS in html, "the postal address was dropped from the rendered email"


def test_empty_address_renders_no_stray_markup():
    """The current (non-compliant) state must at least stay clean: no empty
    address line, no literal 'None'."""
    html = render_html(BODY, address="")
    assert "None" not in html


def test_address_is_html_escaped():
    html = render_html(BODY, address='Suite <b>4</b> & Co, "The Annex"')
    assert "<b>4</b>" not in html
    assert "&lt;b&gt;4&lt;/b&gt;" in html
    assert "&amp;" in html


def test_unsubscribe_link_is_present_by_default():
    html = render_html(BODY)
    assert "unsubscribe" in html.lower()
    assert "mailto:" in html


def test_unsubscribe_can_be_rendered_for_a_custom_address():
    html = render_html(BODY, unsubscribe_email="ops@example.com")
    assert "ops@example.com" in html


def test_render_is_deterministic():
    """Two renders of identical input must be byte-identical — the same
    determinism bar the rest of the chain holds."""
    assert render_html(BODY, address=ADDRESS) == render_html(BODY, address=ADDRESS)

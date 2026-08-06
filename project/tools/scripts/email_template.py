#!/usr/bin/env python3
"""Professional HTML email wrapper for all automatelb.com outreach.

ONE source of truth for how an Automate email looks. Both draft modes
(template + custom) and the Brevo sender render through `render_html()` so
every email that leaves the system shares the same clean, deliverability-safe
shell: a personal 1:1 cold email (NOT a newsletter), with a proper signature
component and a compliant footer.

Design rationale (cold 1:1 outreach, not marketing blast):
  * Single column, max 560px, system fonts, near-black on white. No hero image,
    no banner, no multi-link nav. Heavy branded templates lower reply rates and
    trip spam heuristics on cold mail; a clean signature reads as a real person.
  * Inline CSS only (Gmail/Outlook strip <style> blocks and external CSS).
  * Table-based layout for Outlook's Word rendering engine.
  * The body copy is passed straight through from the drafter (plain, human,
    no em/en dashes per voice-*.md). This module only adds the shell + signature
    + footer, so it never changes the words the gap-writer wrote.

Public API:
    render_html(body_text, *, signoff_name=..., company=..., site=...,
                tagline=..., unsubscribe_email=...) -> str
"""
from __future__ import annotations

import html as _html
import re

# --- brand tokens ---------------------------------------------------------
INK = "#1b2733"        # body text (near-black, warm)
MUTE = "#6b7785"       # footer / secondary
ACCENT = "#2f6f6b"     # links + wordmark (deep teal)
HAIR = "#e6e9ee"       # hairlines / dividers
CARD = "#ffffff"
PAGE = "#f4f5f7"
FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,"
        "sans-serif")

# voice contract: no em/en dashes anywhere in a rendered email.
_DASH_RE = re.compile(r"\s*[—–]\s*")

# Every outreach signature carries the Instagram handle. Matched here so the
# plain-text sig block the drafter appends is stripped before _signature()
# re-renders it as a styled component (no duplicate line in the HTML).
INSTAGRAM = "dave.automates"
INSTAGRAM_RE = re.compile(r"[Ii]nstagram\s*[:@]?\s*@?" + re.escape(INSTAGRAM))


def _no_dash(s: str) -> str:
    return _DASH_RE.sub(", ", s or "")


def _linkify_site(text: str, site: str) -> str:
    """Turn a bare site mention (osgdev.com) in already-escaped text into a link.

    Bare mentions only: an occurrence preceded by '.', '/', or a word char is
    part of a longer URL/subdomain (e.g. video.osgdev.com, already anchored by
    _linkify_urls) and must be left alone.
    """
    if not site:
        return text
    esc = _html.escape(site)
    anchor = (f'<a href="https://{esc}" '
              f'style="color:{ACCENT};text-decoration:none;">{esc}</a>')
    return re.sub(r"(?<![\w./-])" + re.escape(esc) + r"(?![\w-])", anchor, text)


# Full URLs in body copy (e.g. the demo-video link) must be clickable, not
# escaped plain text. Runs on already-escaped text, before _linkify_site;
# trailing sentence punctuation stays outside the anchor.
_URL_RE = re.compile(r"https?://[^\s<]+[^\s<.,;:)!?]")


def _linkify_urls(text: str) -> str:
    return _URL_RE.sub(
        lambda m: (f'<a href="{m.group(0)}" '
                   f'style="color:{ACCENT};text-decoration:none;">{m.group(0)}</a>'),
        text)


def _body_blocks(body_text: str, site: str) -> str:
    """Render the drafter's plain body into <p> blocks, minus the signature.

    The signature ("Name\\nCompany, site") is re-rendered as a styled component,
    so we strip it from the prose body if present to avoid duplication.
    """
    text = _no_dash(body_text.strip())
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]

    # Drop a trailing signature block the drafter may have appended. The
    # Instagram line may land in the same block or as its own trailing block.
    if paras and INSTAGRAM_RE.search(paras[-1]) and len(paras[-1].splitlines()) <= 2:
        paras = paras[:-1]
    if site and paras and site in paras[-1]:
        paras = paras[:-1]

    out = []
    for para in paras:
        safe = _html.escape(para).replace("\n", "<br>")
        safe = _linkify_urls(safe)
        safe = _linkify_site(safe, site)
        out.append(
            f'<p style="margin:0 0 16px;font-family:{FONT};font-size:15px;'
            f'line-height:1.62;color:{INK};">{safe}</p>'
        )
    return "\n".join(out)


def _signature(name: str, company: str, site: str, tagline: str, instagram: str = "") -> str:
    site_esc = _html.escape(site)
    name_esc = _html.escape(name)
    company_esc = _html.escape(company)
    tag = (f'<div style="font-family:{FONT};font-size:13px;line-height:1.5;'
           f'color:{MUTE};margin-top:2px;">{_html.escape(_no_dash(tagline))}</div>'
           if tagline else "")
    ig = ""
    if instagram:
        handle = _html.escape(instagram.lstrip("@"))
        ig = (f'<div style="font-family:{FONT};font-size:14px;color:{MUTE};margin-top:1px;">'
              f'Instagram: <a href="https://instagram.com/{handle}" '
              f'style="color:{ACCENT};text-decoration:none;">{handle}</a></div>')
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:24px 0 0;border-collapse:collapse;">
  <tr>
    <td style="padding-top:18px;border-top:1px solid {HAIR};">
      <div style="font-family:{FONT};font-size:15px;font-weight:600;color:{INK};">{name_esc}</div>
      <div style="font-family:{FONT};font-size:14px;color:{INK};margin-top:1px;">
        <span style="font-weight:600;letter-spacing:.2px;color:{ACCENT};">{company_esc}</span>
        <span style="color:{MUTE};">&nbsp;&middot;&nbsp;</span>
        <a href="https://{site_esc}" style="color:{ACCENT};text-decoration:none;">{site_esc}</a>
      </div>
      {ig}
      {tag}
    </td>
  </tr>
</table>""".strip()


def _footer(company: str, site: str, unsubscribe_email: str, address: str) -> str:
    unsub = ""
    if unsubscribe_email:
        ue = _html.escape(unsubscribe_email)
        unsub = (f'<a href="mailto:{ue}?subject=unsubscribe" '
                 f'style="color:{MUTE};text-decoration:underline;">unsubscribe</a>')
    addr_line = f'{_html.escape(_no_dash(address))}<br>' if address else ""
    reason = (f"You received this one time email because {_html.escape(company)} "
              f"reached out about your business. ")
    return f"""
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="border-collapse:collapse;">
  <tr>
    <td style="padding:18px 0 0;font-family:{FONT};font-size:12px;line-height:1.6;color:{MUTE};">
      {reason}{('If this is not relevant, ' + unsub + ' and you will not hear from us again.') if unsub else ''}
      <br><br>
      {addr_line}<a href="https://{_html.escape(site)}" style="color:{MUTE};text-decoration:underline;">{_html.escape(site)}</a>
    </td>
  </tr>
</table>""".strip()


def render_html(
    body_text: str,
    *,
    signoff_name: str = "David Geha",
    company: str = "OSG",
    site: str = "osgdev.com",
    tagline: str = "We set up quiet AI assistants that take the repetitive inbox and scheduling work off your team.",
    unsubscribe_email: str = "david@osgdev.com",
    address: str = "",
    instagram: str = INSTAGRAM,
) -> str:
    """Wrap a plain-text outreach body in the professional Automate shell.

    `body_text` is the drafter's exact copy (already human, no dashes). Pass the
    signature lines in it or not, either way, this re-renders the signature as a
    styled component and strips a duplicate trailing one.
    """
    blocks = _body_blocks(body_text, site)
    sig = _signature(signoff_name, company, site, tagline, instagram)
    foot = _footer(company, site, unsubscribe_email, address)
    preheader = ""  # body already leads with the hook; no marketing preheader.

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>{_html.escape(company)}</title>
</head>
<body style="margin:0;padding:0;background:{PAGE};">
{preheader}
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:{PAGE};border-collapse:collapse;">
  <tr>
    <td align="center" style="padding:28px 16px;">
      <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="560" style="width:560px;max-width:100%;background:{CARD};border:1px solid {HAIR};border-radius:10px;border-collapse:separate;">
        <tr>
          <td style="padding:34px 36px 30px;">
            {blocks}
            {sig}
            {foot}
          </td>
        </tr>
      </table>
      <div style="font-family:{FONT};font-size:11px;color:{MUTE};opacity:.7;padding:14px 0 0;">{_html.escape(company)} &middot; {_html.escape(site)}</div>
    </td>
  </tr>
</table>
</body>
</html>"""


if __name__ == "__main__":
    sample = (
        "Hello Mr. Freeman,\n\n"
        "Your contact form on fusonlaw.com says \"We will get back to you as soon "
        "as possible,\" which means someone on your team is reading, sorting, and "
        "replying to each incoming inquiry by hand before a consult is ever booked. "
        "For a criminal and family law practice that runs 24/7, that is a lot of "
        "quiet hours spent on follow-up.\n\n"
        "We set up an assistant that reads those intake emails as they come in, "
        "sorts real cases from the noise, books the consult straight into your "
        "calendar, then sends reminders. Not a chatbot. A quiet assistant that "
        "handles the repetitive part so your staff does not.\n\n"
        "Would Tuesday or Thursday afternoon work for a quick 15-minute call?\n\n"
        "David Geha\nOSG, osgdev.com"
    )
    print(render_html(sample))

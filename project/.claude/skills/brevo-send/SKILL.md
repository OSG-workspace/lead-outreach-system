---
name: brevo-send
description: Sends drafted emails via the Brevo MCP server, tracks each send with a Brevo message ID, and updates vault notes to status=sent. Use after outreach-copywriting and after the orchestrator has logged each draft to the vault. Respects MAX_EMAILS_PER_RUN cap and dedup against sent-log.md.
---

# Brevo Send

This is the only skill that fires emails. Treat it as a privileged operation — every send writes a permanent record.

## Prerequisites (verify before sending)

Before the FIRST send of any campaign, run the deliverability gate. If any check below fails, abort the entire campaign and tell the user what to fix.

### A. Domain authentication

1. `BREVO_MCP_TOKEN` is set in the environment
2. `BREVO_SENDER_EMAIL` is set AND has been verified in Brevo (check via Brevo MCP `get-account` / `get-senders`)
3. The sender domain has SPF that includes `sendinblue.com` — `dig +short TXT <sender-domain> | grep "v=spf1"` returns a record containing `include:sendinblue.com`
4. The sender domain has DKIM Brevo selectors — `dig +short TXT brevo1._domainkey.<sender-domain>` returns a non-empty record
5. The sender domain has DMARC — `dig +short TXT _dmarc.<sender-domain>` returns `v=DMARC1`
6. **The sender domain has at least one MX record** OR `vault/lead-outreach/deliverability.md` frontmatter declares `mx_decision: deferred` AND a valid `BREVO_REPLY_TO` env var is set. If MX is empty AND `mx_decision` is not `deferred`, refuse the campaign — replies will bounce and no-MX is a strong spam signal. See `vault/lead-outreach/deliverability.md` for the recommended fix.

### B. Vault and dedup

7. `vault/lead-outreach/deliverability.md` exists, `mx_present: true` is set in its frontmatter, and `warm_up_state` is not `pre_warmup`
8. The vault note at `vault/lead-outreach/leads/<lead-slug>.md` exists with `status: pending_send`
9. The recipient does NOT appear in `vault/lead-outreach/sent-log.md` (verify even if orchestrator deduped)
10. The recipient's email domain has an MX record (otherwise: guaranteed bounce, skip the lead)

### C. Per-send sanity

11. The drafted email passed `outreach-copywriting`'s pre-flight checklist (no banned words, correct salutation, etc.)
12. The recipient address is not a role account regex hit (`^(info|contact|hello|sales|admin|support|noreply|no-reply|marketing|hr|jobs|careers)@`) UNLESS lead score ≥ 85
13. The current time is inside the recipient's business hours (default GCC: Sun–Thu, 09:00–17:00 GST/AST). If not, queue the lead for the next valid slot.
14. The today's daily cap from `deliverability.md` warm-up table has not been exceeded

If any A/B check fails, **abort the campaign** and tell the user. If a C check fails, skip the individual lead and move on.

## Sending procedure

For each lead in `runs/<run-slug>/emails-drafted.json`, send only `step 1` (initial email) on this run. Steps 2-3 are scheduled for future runs.

Use the Brevo MCP `send_email` (or equivalent transactional send tool — likely `send-transactional-email` on the official MCP).

Input:

```json
{
  "sender": {
    "email": "<BREVO_SENDER_EMAIL>",
    "name": "<BREVO_SENDER_NAME>"
  },
  "to": [
    {
      "email": "<lead.to_email>",
      "name": "<lead.to_name>"
    }
  ],
  "replyTo": {
    "email": "<BREVO_REPLY_TO or BREVO_SENDER_EMAIL if unset>",
    "name": "<BREVO_SENDER_NAME>"
  },
  "subject": "<email.subject>",
  "htmlContent": "<email.body_html>",
  "textContent": "<email.body_text>",
  "tags": ["cold-outreach", "<run-slug>", "<icp.target.industry>"],
  "headers": {
    "X-Mailin-Custom": "lead_id=<lead.id>;run=<run-slug>",
    "List-Unsubscribe": "<mailto:<BREVO_SENDER_EMAIL>?subject=remove>",
    "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"
  }
}
```

The `replyTo` field is critical. Set it from `BREVO_REPLY_TO` env var if present; otherwise default to `BREVO_SENDER_EMAIL`. This lets users route replies to a real inbox while sending from a verified-but-not-receiving sender (e.g., a Brevo-verified `david@automatelb.com` that has no MX yet but the user wants replies in their `outlook.com` inbox).

The `List-Unsubscribe` headers are RFC 8058 compliant and dramatically improve Gmail/Outlook deliverability — Google specifically downranks senders that lack them on bulk mail. They also satisfy CAN-SPAM in addition to the in-body opt-out.

**Single-email mode is enforced**: each lead produces exactly one email object in `emails-drafted.json`. There is no `step 2` or `step 3` to send.

The `tags` make it easy to filter performance later in Brevo's analytics. The custom header lets you correlate Brevo events back to your vault notes.

## After each successful send

1. Capture the `messageId` from the Brevo response
2. Update `vault/lead-outreach/leads/<lead-slug>.md` frontmatter:
   ```yaml
   status: sent
   sent_at: 2026-05-06T14:32:11Z
   brevo_message_id: <id>
   sequence_step: 1   # never increments — single-email mode, no follow-ups
   ```
3. Append a line to `vault/lead-outreach/sent-log.md` (use the wikilink form):
   ```
   2026-05-06 | <lead.to_email> | [[<lead-slug>]] | step 1 | <run-slug> | <brevo_message_id>
   ```

> [!important] Single-email mode
> The pipeline does not schedule follow-ups. Do not write a `next_step_due` field, do not append a follow-up draft to `lead-outreach/follow-ups/`, do not increment `sequence_step` after the initial send. One email per lead, lead is then complete.

## Pacing — read warm-up state from `vault/lead-outreach/deliverability.md`

Velocity rules vary by warm-up state. Always read the frontmatter of `deliverability.md` at the start of each campaign:

| `warm_up_state` | Daily cap | Min seconds between sends | Notes |
|---|--:|--:|---|
| `pre_warmup` | 0 | — | refuse to send anything |
| `day_1_3` | 5 | 600 (10 min) | only warm contacts you know |
| `day_4_7` | 10 | 300 (5 min) | qualified leads, score ≥ 80 |
| `week_2` | 15 | 180 (3 min) | qualified leads, score ≥ 75 |
| `week_3` | 20 | 120 (2 min) | qualified leads, score ≥ 70 |
| `steady_state` | 30 | 90 (1.5 min) | full operating mode |

Additional pacing rules:

- **Never send to > 2 emails on the same domain in one run** (catches sourcing runs that grabbed multiple contacts at one company)
- **Never send outside recipient business hours** (default GCC: Sunday–Thursday, 09:00–17:00 GST/AST). If a draft is dequeued outside the window, it waits for the next slot.
- **Never send on Friday or Saturday** (GCC weekend)
- **Pause on bounce-rate spike**: if the running bounce rate for the campaign exceeds 8%, abort the rest of the run and tell the user. Continuing past 8% bounces damages domain reputation for weeks.

## Handling failures

Brevo errors fall into three categories. Handle each differently:

| Error | Handling |
|---|---|
| Invalid recipient (400, bad email format) | Mark lead `status: invalid_email`. Do NOT add to sent-log. Continue with next lead. |
| Sender not verified (401/403) | STOP the entire send batch. Tell the user to verify sender in Brevo. Don't try other leads (same problem will happen). |
| Rate limit (429) | Wait 60 seconds, retry once. If still failing, stop the batch and tell the user. |
| Soft bounce / hard bounce (post-send via webhook) | Update the vault note's `status: bounced`. Add the email to a `vault/lead-outreach/bounce-list.md` to permanently exclude in future runs. |
| Unknown / 5xx | Mark lead `status: send_failed`. Continue to next lead. Tell the user to investigate. |

## What NOT to do

- **Never send to an email that's already in `sent-log.md`** — the orchestrator dedups, but check again here as a safety belt. False positive on dedup is annoying; false negative destroys deliverability.
- **Never use Brevo's bulk campaign endpoint** for cold outreach. Cold outreach must be transactional (one send call per lead) for compliance and deliverability. Bulk endpoints are for opted-in lists.
- **Never include unsubscribe-style merge tags meant for opt-in campaigns.** Cold outreach handles unsubscribe via a plain-text "reply 'remove' and I'll take you off this list" line in the email body — Brevo's transactional flow doesn't auto-add unsubscribe footers, which is correct here.

## Compliance footer

By law (CAN-SPAM in the US, similar in EU/UK), every cold email needs:

- A physical mailing address
- An opt-out mechanism

Read `vault/lead-outreach/compliance.md` for the user's address and preferred opt-out language. Append to every email body before send. If `compliance.md` doesn't exist, STOP and ask the user to create it before sending.

## Output to the orchestrator

After processing the batch, return:

```json
{
  "attempted": 18,
  "sent": 16,
  "skipped": 1,
  "failed": 1,
  "details": [
    { "lead_id": "...", "result": "sent", "message_id": "..." },
    { "lead_id": "...", "result": "skipped", "reason": "already_in_sent_log" },
    { "lead_id": "...", "result": "failed", "reason": "invalid_email" }
  ]
}
```

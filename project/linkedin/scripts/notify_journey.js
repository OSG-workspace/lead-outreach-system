#!/usr/bin/env node
/*
 * THE HUMAN STEP — emails David the leads that went cold.
 *
 * The last step of the journey is deliberately NOT another message to the lead.
 * When someone has taken the outreach and the video and said nothing, the
 * pipeline stops and asks a person what to do. This is that hand-off.
 *
 * Brevo REST, the same free-tier account and the same key the email channel
 * already uses (BREVO_MCP_TOKEN in project/.env) — no new dependency, no new
 * cost. One digest email per run, never one per lead, and every notified lead
 * is stamped `notifiedAt` in the queue so a second run that day sends nothing.
 *
 * USAGE  node scripts/notify_journey.js            # DRY RUN — prints the digest
 *        node scripts/notify_journey.js --send
 */
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { paths } from '../lib/paths.js';
import { alert } from '../lib/alerts.js';

const NOTIFY_QUEUE = path.join(path.dirname(paths.state), 'notify-queue.json');
const ENDPOINT = 'https://api.brevo.com/v3/smtp/email';
const TO = process.env.JOURNEY_NOTIFY_TO || 'david@osgdev.com';

/** project/.env — same file the Python senders read; no dotenv dependency. */
export function loadEnv(file = path.resolve(paths.root, '..', '.env')) {
  if (!fs.existsSync(file)) return {};
  const out = {};
  for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
    const m = /^([A-Z0-9_]+)=(.*)$/.exec(line.trim());
    if (m) out[m[1]] = m[2].replace(/^["']|["']$/g, '');
  }
  return out;
}

/** Brevo keys are sometimes handed over base64-wrapped; mirrors send_batch_brevo.py. */
export function decodeBrevoKey(token = '') {
  if (token.startsWith('xkeysib-') && !token.endsWith('==')) return token;
  try {
    const body = token.startsWith('xkeysib-') ? token.slice('xkeysib-'.length) : token;
    return JSON.parse(Buffer.from(`${body}==`, 'base64').toString())?.api_key || token;
  } catch { return token; }
}

/** The cloud's view: cold leads out of the committed digest. */
export function coldFromDigest(digest) {
  return (digest?.cold || []).map((c) => ({ ...c, dueAt: c.since }));
}

/**
 * The stalled-channel email. A different message from "these leads went cold":
 * nothing is wrong with the leads, the sender did not run. It names what is
 * waiting so David can decide whether to open the laptop today.
 */
export function renderStalled({ hours, overdue }) {
  const rows = overdue.slice(0, 25).map((o) => {
    const who = [o.name, o.company].filter(Boolean).join(' — ') || o.profileUrl;
    return `<li><a href="${o.profileUrl}">${who}</a> — <b>${o.step}</b> due ${String(o.dueAt).slice(0, 10)}</li>`;
  }).join('\n');
  return {
    subject: `LinkedIn drip stalled — ${hours}h since the last run, ${overdue.length} step(s) waiting`,
    html: `<p>The Mac has not run the LinkedIn drip for <b>${hours} hours</b>. Sending needs the logged-in Chrome on that machine, so nothing has gone out.</p>` +
          (overdue.length ? `<p>Waiting on it:</p>\n<ul>\n${rows}\n</ul>` : '<p>Nothing is due yet, so no harm done — but the channel is not running.</p>') +
          `<p><small>Nothing is lost: due dates are derived, so every step above goes out in order on the next run. Open the Mac and it catches up by itself.</small></p>`,
  };
}

export function pending(queue) {
  return queue.filter((e) => !e.notifiedAt);
}

export function renderDigest(entries) {
  const rows = entries.map((e) => {
    const who = [e.name, e.company].filter(Boolean).join(' — ') || e.profileUrl;
    return `<li><a href="${e.profileUrl}">${who}</a><br><small>outreach + video sent, no reply (cold since ${String(e.dueAt).slice(0, 10)})</small></li>`;
  }).join('\n');
  return {
    subject: `LinkedIn journey: ${entries.length} lead${entries.length === 1 ? '' : 's'} went cold`,
    html: `<p>These people took the connection, read (or ignored) the outreach and the video, and said nothing. The pipeline has stopped messaging them — they need your call.</p>\n<ul>\n${rows}\n</ul>\n<p><small>Sent by scripts/notify_journey.js. Mark someone as replied with:<br><code>node scripts/journey_tick.js --mark-replied &lt;profile-url&gt;</code></small></p>`,
  };
}

export async function sendDigest({ apiKey, sender, entries, to = TO, fetchImpl = fetch, body = null }) {
  const { subject, html } = body || renderDigest(entries);
  const res = await fetchImpl(ENDPOINT, {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json', 'api-key': apiKey },
    body: JSON.stringify({ sender, to: [{ email: to }], subject, htmlContent: html }),
  });
  if (!res.ok) throw new Error(`brevo ${res.status}: ${(await res.text()).slice(0, 300)}`);
  return res.json().catch(() => ({}));
}

/** Key from the environment (cloud) or project/.env (Mac) — in that order. */
export function brevoCreds() {
  const env = { ...loadEnv(), ...process.env };
  return {
    apiKey: decodeBrevoKey(process.env.BREVO_MCP_TOKEN || env.BREVO_MCP_TOKEN || ''),
    sender: { email: env.BREVO_SENDER_EMAIL, name: env.BREVO_SENDER_NAME || 'OSG' },
  };
}

async function mailOrDie({ entries, body }) {
  const { apiKey, sender } = brevoCreds();
  if (!apiKey || !sender.email) {
    alert(paths.alerts, 'ERROR', 'journey notification not sent: BREVO_MCP_TOKEN / BREVO_SENDER_EMAIL not available here');
    process.exit(1);
  }
  try {
    await sendDigest({ apiKey, sender, entries, body });
  } catch (e) {
    alert(paths.alerts, 'ERROR', `journey notification failed: ${e.message}`);
    process.exit(1);
  }
}

/**
 * THE CLOUD PATH. It has the repo and nothing else — no state/, no browser —
 * so every decision here comes from the committed digest.
 */
async function fromDigest(send) {
  const { readDigest, staleness } = await import('./journey_digest.js');
  const digest = readDigest();
  if (!digest) { console.log('[notify] no digest committed yet — the Mac has never published'); return; }
  const s = staleness(digest);
  const stalled = process.argv.includes('--stalled') || s.stale;
  const cold = coldFromDigest(digest);

  if (stalled) {
    const body = renderStalled({ hours: s.hours, overdue: digest.overdue || [] });
    console.log(`[notify] STALLED ${s.hours}h, ${digest.overdue.length} step(s) waiting -> ${TO}`);
    if (send) await mailOrDie({ entries: [], body });
    return;
  }
  if (!cold.length) { console.log(`[notify] healthy — last run ${s.hours}h ago, nothing cold`); return; }
  console.log(`[notify] ${cold.length} cold lead(s) -> ${TO}`);
  if (send) await mailOrDie({ entries: cold });
}

async function main() {
  const send = process.argv.includes('--send');
  if (process.argv.includes('--from-digest')) {
    await fromDigest(send);
    if (!send) console.log('   (dry run — pass --send to email)');
    return;
  }
  const queue = fs.existsSync(NOTIFY_QUEUE) ? JSON.parse(fs.readFileSync(NOTIFY_QUEUE, 'utf8')) : [];
  const todo = pending(queue);
  if (!todo.length) { console.log('[notify] nothing cold to report'); return; }

  console.log(`[notify] ${todo.length} cold lead(s) -> ${TO}`);
  for (const e of todo) console.log(`  ${e.name ?? e.profileUrl} (${e.company ?? '—'})`);
  if (!send) { console.log('   (dry run — pass --send to email)'); return; }

  await mailOrDie({ entries: todo });
  const at = new Date().toISOString();
  for (const e of todo) e.notifiedAt = at;   // stamped only after Brevo accepted it
  fs.writeFileSync(NOTIFY_QUEUE, JSON.stringify(queue, null, 2));
  console.log(`[notify] emailed ${todo.length} cold lead(s) to ${TO}`);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main();

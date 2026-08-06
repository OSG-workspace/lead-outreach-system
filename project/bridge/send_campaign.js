// project/bridge/send_campaign.js
//
// Outbound WhatsApp sender for the lead-outreach pipeline. Reuses the same
// whatsapp-web.js LocalAuth session as the interactive bridge (.wwebjs_auth)
// so no second QR scan is needed.
//
// Usage:
//   node send_campaign.js --run-dir <abs-path-to-runs/YYYY-MM-DD-slug> [--send] [--cap N] [--delay-ms MS]
//
// Reads <run-dir>/whatsapp-drafted.json (one JSON per line, produced by
// tools/scripts/draft_whatsapp.py) and sends each entry to its `to_jid`.
// Writes <run-dir>/whatsapp-sent.jsonl and appends a line to
// <run-dir>/whatsapp-send-log.txt per attempt.
//
// Defaults to DRY-RUN: prints what it would send, ships nothing. Pass --send
// to actually send. Honors WHATSAPP_MAX_PER_RUN env (default 200) and a
// per-message delay (--delay-ms, default 4000) to avoid pacing flags.

import pkg from 'whatsapp-web.js';
const { Client, LocalAuth } = pkg;
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { readFileSync, writeFileSync, appendFileSync, existsSync } from 'node:fs';
import 'dotenv/config';

const __dirname = dirname(fileURLToPath(import.meta.url));

function parseArgs(argv) {
  const out = { send: false, cap: null, delayMs: null, runDir: null, sentLog: null };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--send') out.send = true;
    else if (a === '--run-dir') out.runDir = argv[++i];
    else if (a === '--cap') out.cap = Number(argv[++i]);
    else if (a === '--delay-ms') out.delayMs = Number(argv[++i]);
    else if (a === '--sent-log') out.sentLog = argv[++i];
  }
  if (!out.runDir) {
    console.error('Usage: node send_campaign.js --run-dir <abs-path> [--send] [--cap N] [--delay-ms MS]');
    process.exit(2);
  }
  return out;
}

const args = parseArgs(process.argv);
const CAP = args.cap ?? Number(process.env.WHATSAPP_MAX_PER_RUN || 200);
const DELAY_MS = args.delayMs ?? Number(process.env.WHATSAPP_DELAY_MS || 4000);
const DRAFTS_FILE = join(args.runDir, 'whatsapp-drafted.json');
const SENT_FILE = join(args.runDir, 'whatsapp-sent.jsonl');
const LOG_FILE = join(args.runDir, 'whatsapp-send-log.txt');
// Master cross-run dedup log. runDir is <repo>/project/runs/<slug> and the ONE
// canonical vault is <repo>/project/vault (see CLAUDE.md) — i.e. two levels up,
// not three. This was `'..','..','..'`, which resolved to a path that does not
// exist; existsSync() then silently left the dedup set empty and the cross-run
// WhatsApp net was disabled outright. Override with --sent-log if needed.
const SENT_LOG_FILE =
  args.sentLog ?? join(args.runDir, '..', '..', 'vault', 'lead-outreach', 'sent-log.md');

if (!existsSync(DRAFTS_FILE)) {
  console.error(`ABORT: ${DRAFTS_FILE} missing. Run tools/scripts/draft_whatsapp.py first.`);
  process.exit(3);
}

const drafts = readFileSync(DRAFTS_FILE, 'utf8')
  .split('\n')
  .map((l) => l.trim())
  .filter(Boolean)
  .map((l) => JSON.parse(l));

if (!drafts.length) {
  console.error('ABORT: 0 drafts in whatsapp-drafted.json');
  process.exit(4);
}

// Dedup against prior sends in this run folder (re-run safety).
const alreadySent = new Set();
if (existsSync(SENT_FILE)) {
  for (const line of readFileSync(SENT_FILE, 'utf8').split('\n')) {
    if (!line.trim()) continue;
    try {
      const r = JSON.parse(line);
      if (r.result === 'sent' && r.to_jid) alreadySent.add(r.to_jid);
    } catch {}
  }
}

// Cross-run dedup: a phone already in the master sent-log (this or any past
// run, WhatsApp channel) must never be messaged again. Final send-time net.
// Kill-on-fallback: a missing ledger must ABORT, never silently degrade into
// "no dedup" — that is how a previously contacted lead gets messaged twice.
if (!existsSync(SENT_LOG_FILE)) {
  console.error(
    `ABORT: master sent-log not found at ${SENT_LOG_FILE}.\n` +
      '  Cross-run WhatsApp dedup cannot be enforced, so nothing was sent.\n' +
      '  Pass --sent-log <path> if the vault lives elsewhere.'
  );
  process.exit(8);
}
const sentPhones = new Set();
{
  const waRe = /wa:(\d{6,15})/g;
  for (const line of readFileSync(SENT_LOG_FILE, 'utf8').split('\n')) {
    let m;
    while ((m = waRe.exec(line)) !== null) sentPhones.add(m[1]);
  }
}

const queue = drafts
  .filter((d) => !alreadySent.has(d.to_jid) && !sentPhones.has(String(d.to_phone)))
  .slice(0, CAP);
const crossRunSkipped = drafts.filter((d) => sentPhones.has(String(d.to_phone))).length;
console.log(
  `${args.send ? '[LIVE]' : '[DRY-RUN]'} Sending ${queue.length} WhatsApp messages ` +
    `(cap=${CAP}, delay=${DELAY_MS}ms, this-run skipped=${alreadySent.size}, ` +
    `prior-run skipped=${crossRunSkipped}).`
);

if (!args.send) {
  for (const d of queue.slice(0, 5)) {
    console.log(`\n--- ${d.to_jid} | ${d.to_name} | salutation=${d.salutation} ---`);
    console.log(d.body_text);
  }
  console.log(`\nDRY-RUN done. Pass --send to actually ship.`);
  process.exit(0);
}

// Campaign sends own the WhatsApp session outright. WhatsApp is used ONLY during
// runs (user directive 2026-07-27): the auto-replying chat bridge (index.js) is
// opt-in and must stay off, so nothing else holds this profile. Chrome refuses to
// open one user-data-dir twice, so if the chat bridge is ever left running it
// locks this sender out ("The browser is already running for ...") and the send
// dies. Keep index.js off and this session is always free and already linked.
const client = new Client({
  authStrategy: new LocalAuth({ dataPath: join(__dirname, '.wwebjs_auth') }),
  puppeteer: { headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox'] },
});

// Never hang waiting for a scan mid-campaign: if the session is not linked,
// abort loudly with the exact fix (kill-on-fallback).
client.on('qr', async () => {
  console.error(
    'ABORT: WhatsApp session is not linked (needs a one-time QR scan).\n' +
      '  Run:  node bridge/link_whatsapp.js   then scan with WhatsApp > Linked Devices.\n' +
      '  Nothing was sent.'
  );
  try {
    await client.destroy();
  } finally {
    process.exit(9);
  }
});

let stats = { attempted: 0, sent: 0, failed: 0 };

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function logLine(obj) {
  const ts = new Date().toISOString();
  appendFileSync(LOG_FILE, `[${ts}] ${JSON.stringify(obj)}\n`);
  appendFileSync(SENT_FILE, JSON.stringify({ ts, ...obj }) + '\n');
}

// WhatsApp has migrated contacts to LID addressing: getNumberId('<msisdn>')
// now returns `<opaque-id>@lid`, not `<msisdn>@c.us`. whatsapp-web.js
// sends fine either way (getChat resolves the c.us wid to the same chat), BUT
// its `sendMessage` ends with `Msg.get(newMsgKey._serialized)`, and that lookup
// misses when the chat is LID-addressed — so it returns `undefined` for a
// message that WAS actually delivered (verified: ack=2 in the Msg store).
//
// That false negative is worse than a failed send: the lead gets the message,
// the run records `failed`, no `wa:<phone>` lands in the sent-log, and a retry
// messages a real prospect twice. So we never infer failure from a missing
// return value — we ask the store whether the message is really there.
async function verifyDelivered(chatId, bodyText, sinceUnix) {
  const needle = String(bodyText).slice(0, 40);
  try {
    return await client.pupPage.evaluate(
      (cid, needle, since) => {
        const Coll = window.require('WAWebCollections');
        const all = Coll.Msg.getModelsArray
          ? Coll.Msg.getModelsArray()
          : Array.from(Coll.Msg.models || []);
        // Match on direction + recency + body. We deliberately do NOT require
        // the chat id to match: the stored message is keyed by the LID form
        // while `cid` may be the c.us form, and that mismatch is the very bug
        // we are working around. Bodies are per-lead personalized (they open
        // with the recipient's own name), so a recent outbound message opening
        // with this exact text is unambiguous.
        const hit = all.find(
          (m) =>
            m.id?.fromMe &&
            (m.t || 0) >= since - 60 &&
            String(m.body || '').startsWith(needle)
        );
        return hit ? { found: true, ack: hit.ack ?? null, id: hit.id?._serialized || '' } : { found: false };
      },
      chatId,
      needle,
      sinceUnix
    );
  } catch (e) {
    return { found: false, probeError: e.message };
  }
}

client.on('ready', async () => {
  console.log('WhatsApp ready, beginning batch send.');
  for (const d of queue) {
    stats.attempted++;
    // Pre-flight: verify the JID actually has WhatsApp before sending.
    let registered = false;
    let targetJid = d.to_jid;
    try {
      const numberId = await client.getNumberId(d.to_phone);
      registered = !!numberId;
      // Address the contact the way WhatsApp itself resolves it (may be @lid).
      if (numberId?._serialized) targetJid = numberId._serialized;
    } catch (e) {
      logLine({
        result: 'failed',
        reason: `getNumberId error: ${e.message}`,
        lead_id: d.lead_id,
        to_jid: d.to_jid,
        to_name: d.to_name,
      });
      stats.failed++;
      await sleep(DELAY_MS);
      continue;
    }
    if (!registered) {
      logLine({
        result: 'failed',
        reason: 'number not on WhatsApp',
        lead_id: d.lead_id,
        to_jid: d.to_jid,
        to_name: d.to_name,
      });
      stats.failed++;
      await sleep(DELAY_MS);
      continue;
    }
    const sentAt = Math.floor(Date.now() / 1000);
    const record = {
      lead_id: d.lead_id,
      to_jid: targetJid,
      to_phone: d.to_phone,
      to_name: d.to_name,
      score: d.score,
      vertical: d.vertical,
      country_code: d.country_code,
      tags: d.tags,
    };
    try {
      // NOTE: `sent` is undefined for LID-addressed chats even on success.
      const sent = await client.sendMessage(targetJid, d.body_text);
      let messageId = sent?.id?._serialized || '';
      if (!messageId) {
        const v = await verifyDelivered(targetJid, d.body_text, sentAt);
        if (!v.found) {
          logLine({
            result: 'failed',
            reason: 'sendMessage returned no message and none found in store',
            ...record,
          });
          stats.failed++;
          await sleep(DELAY_MS);
          continue;
        }
        messageId = v.id;
      }
      logLine({ result: 'sent', message_id: messageId, ...record });
      stats.sent++;
      console.log(`  sent → ${d.to_phone} (${d.to_name})`);
    } catch (e) {
      // A throw is not proof of non-delivery either — confirm against the store
      // before recording a failure, so we never re-message a reached lead.
      const v = await verifyDelivered(targetJid, d.body_text, sentAt);
      if (v.found) {
        logLine({ result: 'sent', message_id: v.id, delivered_despite_error: e.message, ...record });
        stats.sent++;
        console.log(`  sent → ${d.to_phone} (${d.to_name}) [delivered despite: ${e.message}]`);
      } else {
        logLine({ result: 'failed', reason: `sendMessage error: ${e.message}`, ...record });
        stats.failed++;
      }
    }
    await sleep(DELAY_MS);
  }
  console.log(`\nDONE: attempted=${stats.attempted} sent=${stats.sent} failed=${stats.failed} mode=LIVE`);
  await client.destroy();
  process.exit(stats.failed > 0 && stats.sent === 0 ? 6 : 0);
});

client.on('auth_failure', (m) => {
  console.error('AUTH_FAILURE — bridge auth invalid. Run `npm start` in project/bridge once to scan a QR.');
  console.error(m);
  process.exit(5);
});

client.on('disconnected', (r) => console.log(`Disconnected: ${r}`));

client.initialize();

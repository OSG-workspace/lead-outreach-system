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
// Master cross-run dedup log. runDir is <repo>/project/runs/<slug>; the vault
// lives at <repo>/vault. Override with --sent-log if needed.
const SENT_LOG_FILE =
  args.sentLog ?? join(args.runDir, '..', '..', '..', 'vault', 'lead-outreach', 'sent-log.md');

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
const sentPhones = new Set();
if (existsSync(SENT_LOG_FILE)) {
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

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: join(__dirname, '.wwebjs_auth') }),
  puppeteer: { headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox'] },
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

client.on('ready', async () => {
  console.log('WhatsApp ready, beginning batch send.');
  for (const d of queue) {
    stats.attempted++;
    // Pre-flight: verify the JID actually has WhatsApp before sending.
    let registered = false;
    try {
      const numberId = await client.getNumberId(d.to_phone);
      registered = !!numberId;
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
    try {
      const sent = await client.sendMessage(d.to_jid, d.body_text);
      logLine({
        result: 'sent',
        lead_id: d.lead_id,
        to_jid: d.to_jid,
        to_name: d.to_name,
        message_id: sent.id?._serialized || '',
        score: d.score,
        vertical: d.vertical,
        country_code: d.country_code,
        tags: d.tags,
      });
      stats.sent++;
      console.log(`  sent → ${d.to_phone} (${d.to_name})`);
    } catch (e) {
      logLine({
        result: 'failed',
        reason: `sendMessage error: ${e.message}`,
        lead_id: d.lead_id,
        to_jid: d.to_jid,
        to_name: d.to_name,
      });
      stats.failed++;
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

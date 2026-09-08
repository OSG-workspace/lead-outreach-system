#!/usr/bin/env node
/*
 * THE BRIDGE BETWEEN THE MAC AND THE CLOUD.
 *
 * state/ is gitignored and lives only on this laptop, so a cloud routine can
 * see none of it. This writes the one small, committable file that a cloud
 * supervisor CAN see: what is due, what has gone cold, when the Mac last
 * actually ran. `ops/journey-digest.json` is tracked on purpose.
 *
 * WHAT GOES IN IT, AND WHAT NEVER DOES
 * Names, companies and public LinkedIn profile URLs — the same class of data
 * already tracked in li-search/results/. NEVER an email address, a phone
 * number, or the text of a message: those are the fields the pre-commit guard
 * and the vault exist to keep out of git, and a digest is not a loophole.
 *
 * The cloud routine's whole job is derived from two numbers here: `lastRunAt`
 * (has the Mac run?) and `overdue` (what did it miss?).
 *
 * USAGE  node scripts/journey_digest.js            # write it
 *        node scripts/journey_digest.js --check    # read it, exit 1 if stale
 */
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { paths } from '../lib/paths.js';
import { loadState } from '../lib/state.js';
import { loadJourney, evaluate } from '../lib/journey.js';

const JOURNEY_FILE = process.env.LINKEDIN_JOURNEY_FILE || path.join(paths.root, 'config', 'journey.json');
export const DIGEST = path.join(paths.root, 'ops', 'journey-digest.json');
const NOTIFY_QUEUE = path.join(path.dirname(paths.state), 'notify-queue.json');
export const STALE_HOURS = 36;   // one weekday gap is fine; two is a stalled channel

/** Only ever these fields. See the header. */
function scrub(lead) {
  return {
    name: lead.name ?? null,
    company: lead.company ?? null,
    profileUrl: lead.profileUrl ?? null,
  };
}

export function buildDigest({ state, journey, notifyQueue = [], now = Date.now() }) {
  const leads = Object.values(state.leads || {});
  const overdue = [];
  for (const lead of leads) {
    if (!lead.journey?.entryAt) continue;
    const v = evaluate(lead, journey, { now });
    if (v.due) overdue.push({ ...scrub(lead), step: v.step.id, dueAt: v.dueAt });
  }
  const count = (fn) => leads.filter(fn).length;
  return {
    generatedAt: new Date(now).toISOString(),
    // The Mac's own last successful send day — the cloud's staleness signal.
    lastRunAt: new Date(now).toISOString(),
    counts: {
      invitesPending: count((l) => l.status === 'sent'),
      accepted: count((l) => l.status === 'accepted'),
      inJourney: count((l) => l.journey?.status === 'active'),
      replied: count((l) => Boolean(l.repliedAt)),
      done: count((l) => l.journey?.status === 'done'),
    },
    // Due RIGHT NOW and still unsent: if this is non-empty in a digest that is
    // also stale, the cloud routine has something real to shout about.
    overdue: overdue.sort((a, b) => a.dueAt.localeCompare(b.dueAt)),
    cold: notifyQueue.filter((e) => !e.notifiedAt).map((e) => ({
      name: e.name ?? null, company: e.company ?? null, profileUrl: e.profileUrl, since: e.dueAt ?? null,
    })),
  };
}

export function readDigest(file = DIGEST) {
  return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : null;
}

export function staleness(digest, now = Date.now()) {
  const t = Date.parse(digest?.lastRunAt || '');
  if (!Number.isFinite(t)) return { stale: true, hours: Infinity };
  const hours = (now - t) / 3600000;
  return { stale: hours > STALE_HOURS, hours: Math.round(hours) };
}

function main() {
  if (process.argv.includes('--check')) {
    const d = readDigest();
    if (!d) { console.log('[digest] none written yet'); process.exit(1); }
    const s = staleness(d);
    console.log(`[digest] last run ${s.hours}h ago — ${d.overdue.length} overdue, ${d.cold.length} cold, ` +
                `${d.counts.inJourney} in journey`);
    process.exit(s.stale ? 1 : 0);
  }
  const journey = loadJourney(fs.readFileSync(JOURNEY_FILE, 'utf8'));
  const digest = buildDigest({
    state: loadState(paths.state),
    journey,
    notifyQueue: fs.existsSync(NOTIFY_QUEUE) ? JSON.parse(fs.readFileSync(NOTIFY_QUEUE, 'utf8')) : [],
  });
  fs.mkdirSync(path.dirname(DIGEST), { recursive: true });
  fs.writeFileSync(DIGEST, JSON.stringify(digest, null, 2));
  console.log(`[digest] wrote ${path.relative(paths.root, DIGEST)} — ${digest.overdue.length} overdue, ${digest.cold.length} cold`);
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) main();

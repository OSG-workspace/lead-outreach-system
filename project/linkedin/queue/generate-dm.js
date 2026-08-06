#!/usr/bin/env node
/*
 * REASONING LAYER for DMs. Mirrors queue/generate.js: reads leads, filters,
 * writes state/dm-queue.json, and stops. It never opens a browser and never
 * sends. messenger.js re-checks every limit itself.
 *
 * WHO ENDS UP IN THE QUEUE
 * Two populations, and the distinction matters:
 *
 *   1. ACCEPTED — people who took an invite from sender.js. state.json marks
 *      them 'sent' and, once the acceptance sweep sees a 1st-degree badge,
 *      accepted. These are the intended DM population: the consent gate has
 *      already been passed.
 *   2. DIRECTLY MESSAGEABLE — Open Profile members and existing 1st-degree
 *      connections, who can be messaged with no invite at all. Supply these
 *      with --leads-file; `directMessageable: true` marks them.
 *
 * Anyone else CANNOT be cold-DMed on LinkedIn without spending an InMail
 * credit. This file does not pretend otherwise: a lead that is neither
 * accepted nor flagged directly-messageable is rejected here with a reason,
 * and messenger.js independently re-checks by looking for the Message button.
 *
 * Every message must be materially different — the same near-duplicate rule
 * the invite notes get. A templated DM from a new connection is the single
 * most obvious automation tell on this channel.
 *
 * USAGE  node queue/generate-dm.js --leads-file <file.json> [--force]
 */
import fs from 'node:fs';
import path from 'node:path';
import { paths } from '../lib/paths.js';
import { loadLimits, dateKeyInTz, isSendDay } from '../lib/limits.js';
import { loadState, normalizeProfileUrl } from '../lib/state.js';
import { loadSuppression } from '../lib/suppression.js';
import { buildSchedule } from '../lib/pacing.js';
import { findNearDuplicates } from '../lib/notes.js';
import { alert } from '../lib/alerts.js';

const DM_QUEUE = paths.dmQueue;
const MAX_DM_CHARS = 1900;   // LinkedIn hard-caps a message; stay well inside

export function validateMessage(msg) {
  const errs = [];
  const m = (msg ?? '').trim();
  if (!m) errs.push('message is empty');
  if (m.length > MAX_DM_CHARS) errs.push(`message is ${m.length} chars (max ${MAX_DM_CHARS})`);
  return errs;
}

export function generateDmQueue({ limits, state, suppression, leads, now = new Date(), rng = Math.random }) {
  const rejected = [];
  const seen = new Set();
  const eligible = [];

  for (const lead of leads) {
    const url = normalizeProfileUrl(lead.linkedinUrl || lead.profileUrl);
    if (!url) { rejected.push({ lead, reason: 'missing or invalid LinkedIn profile URL' }); continue; }
    if (seen.has(url)) { rejected.push({ lead, reason: 'duplicate profile URL' }); continue; }

    const blocked = suppression.blocks({ ...lead, profileUrl: url });
    if (blocked) { rejected.push({ lead, reason: blocked }); continue; }

    const prior = state.leads[url] || {};
    if (prior.dmStatus === 'sent') { rejected.push({ lead, reason: 'already DMed' }); continue; }
    if (prior.dmStatus === 'not-messageable') {
      rejected.push({ lead, reason: 'previously found not messageable' }); continue;
    }

    // The consent gate: accepted, or independently messageable. Nothing else.
    const accepted = prior.accepted === true || prior.degree === 1 || prior.status === 'accepted';
    const direct = lead.directMessageable === true || lead.openProfile === true;
    if (!accepted && !direct) {
      rejected.push({ lead, reason: 'not accepted and not directly messageable — invite first' });
      continue;
    }

    const errs = validateMessage(lead.message);
    if (errs.length) { rejected.push({ lead, reason: errs.join('; ') }); continue; }

    seen.add(url);
    eligible.push({
      leadId: lead.leadId ?? null,
      name: lead.name ?? null,
      company: lead.company ?? null,
      title: lead.title ?? null,
      score: Number(lead.score ?? 0),
      profileUrl: url,
      message: lead.message.trim(),
      via: accepted ? 'accepted' : 'open-profile',
    });
  }

  eligible.sort((a, b) => b.score - a.score);
  const cap = limits.followUpDmsPerDay ?? 25;
  const selected = eligible.slice(0, cap);

  const dupes = findNearDuplicates(selected.map((l) => l.message));
  if (dupes.length) {
    const err = new Error(
      `messages are not materially different: ${dupes
        .map((d) => `#${d.i}~#${d.j} (jaccard ${d.similarity}, shared opening ${d.sharedOpeningWords}w)`)
        .join(', ')}`);
    err.nearDuplicates = dupes;
    throw err;
  }

  const schedule = buildSchedule(limits, selected.length, now, rng);
  return {
    generatedAt: now.toISOString(),
    date: dateKeyInTz(now, limits.timezone),
    timezone: limits.timezone,
    cap,
    entries: selected.map((l, i) => ({ ...l, scheduledAt: schedule[i].toISOString(), channel: 'linkedin-dm' })),
    rejected,
  };
}

/**
 * Rebuild the lead list from state.json.
 *
 * The DM is composed at fire time but sent only once the invite is accepted,
 * which is days or weeks later — by then the run folder that held
 * linkedin-leads.json is long gone (run_fire cleans it up). sender.js therefore
 * stores each composed DM on the lead itself, and this is how it comes back.
 */
export function leadsFromState(state) {
  return Object.values(state.leads || {})
    .filter((l) => l.status === 'accepted' && l.message && l.dmStatus !== 'sent')
    .map((l) => ({
      leadId: l.leadId ?? null,
      name: l.name ?? null,
      company: l.company ?? null,
      title: l.title ?? null,
      score: l.score ?? 0,
      linkedinUrl: l.profileUrl,
      message: l.message,
      directMessageable: true,      // an accepted invite IS the consent gate
    }));
}

async function main() {
  const argv = process.argv.slice(2);
  const idx = argv.indexOf('--leads-file');
  const fromState = argv.includes('--from-state');
  if (idx === -1 && !fromState) {
    console.error('usage: node queue/generate-dm.js --leads-file <file.json>');
    console.error('       node queue/generate-dm.js --from-state   # people who accepted');
    process.exit(2);
  }
  const limits = loadLimits();
  const state = loadState(paths.state);
  const suppression = loadSuppression([paths.suppression, paths.bounces]);
  const leads = fromState
    ? leadsFromState(state)
    : JSON.parse(fs.readFileSync(argv[idx + 1], 'utf8'));
  if (fromState) console.log(`[generate-dm] ${leads.length} accepted people with a composed DM`);
  const now = new Date();

  const sendDay = isSendDay(limits, now) || argv.includes('--force');
  let queue;
  try {
    queue = sendDay
      ? generateDmQueue({ limits, state, suppression, leads, now })
      : { generatedAt: now.toISOString(), date: dateKeyInTz(now, limits.timezone), timezone: limits.timezone, cap: 0, entries: [], rejected: [] };
  } catch (e) {
    alert(paths.alerts, 'ERROR', `dm queue generation failed: ${e.message}`, { nearDuplicates: e.nearDuplicates });
    process.exit(1);
  }

  fs.mkdirSync(path.dirname(DM_QUEUE), { recursive: true });
  fs.writeFileSync(DM_QUEUE, JSON.stringify(queue, null, 2));
  console.log(`[generate-dm] ${queue.entries.length} queued (cap ${queue.cap}), ${queue.rejected.length} rejected`);
  for (const r of queue.rejected.slice(0, 10)) {
    console.log(`  rejected: ${r.lead?.name ?? r.lead?.linkedinUrl ?? '?'} — ${r.reason}`);
  }
}

if (import.meta.url === `file://${process.argv[1]}`) main();

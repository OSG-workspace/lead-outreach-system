#!/usr/bin/env node
/*
 * REASONING LAYER. Reads the CRM, ranks, drafts notes, writes queue.json.
 * It never opens a browser and it never sends. It is also not trusted with the
 * limits: it writes at most today's ramp cap, and sender.js re-checks every
 * limit independently before it clicks anything.
 */
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { paths } from '../lib/paths.js';
import { loadLimits, capForDay, isSendDay, dateKeyInTz } from '../lib/limits.js';
import { loadState, normalizeProfileUrl, campaignDay } from '../lib/state.js';
import { loadSuppression } from '../lib/suppression.js';
import { buildSchedule } from '../lib/pacing.js';
import { findNearDuplicates, validateNote } from '../lib/notes.js';
import { alert } from '../lib/alerts.js';

/** Default lead source: headless Claude with MCP discovery on (never --bare). */
function claudeLeadSource(promptFile) {
  const prompt = fs.readFileSync(promptFile, 'utf8');
  const raw = execFileSync('claude', ['-p', prompt, '--output-format', 'text'], {
    encoding: 'utf8',
    maxBuffer: 32 * 1024 * 1024,
  });
  const start = raw.indexOf('[');
  const end = raw.lastIndexOf(']');
  if (start === -1 || end === -1) throw new Error('lead source returned no JSON array');
  return JSON.parse(raw.slice(start, end + 1));
}

export function generateQueue({
  limits,
  state,
  suppression,
  leads,
  now = new Date(),
  rng = Math.random,
  withNotes = false,
}) {
  const rejected = [];
  const day = campaignDay(state, limits.timezone, now);
  const cap = capForDay(limits, day);

  const seen = new Set();
  const eligible = [];

  for (const lead of leads) {
    const url = normalizeProfileUrl(lead.linkedinUrl || lead.profileUrl);
    if (!url) { rejected.push({ lead, reason: 'missing or invalid LinkedIn profile URL' }); continue; }
    if (seen.has(url)) { rejected.push({ lead, reason: 'duplicate profile URL' }); continue; }

    const blocked = suppression.blocks({ ...lead, profileUrl: url });
    if (blocked) { rejected.push({ lead, reason: blocked }); continue; }

    const prior = state.leads[url];
    if (prior && prior.status !== 'queued') {
      rejected.push({ lead, reason: `already ${prior.status} on LinkedIn` });
      continue;
    }

    // Someone already connected, or an Open Profile member, needs no invite at
    // all — they can be messaged today. Queueing an invite for them would spend
    // one of the day's 8-18 on a person who is already reachable, and for an
    // existing 1st-degree connection there is no Connect button to click.
    // generate-dm.js picks these up instead.
    if (lead.directMessageable === true || lead.openProfile === true || lead.degree === 1) {
      rejected.push({ lead, reason: 'already reachable (1st-degree or open profile) — DM directly, no invite needed' });
      continue;
    }

    // NOTE-LESS BY DEFAULT, because of what a free account actually allows.
    // LinkedIn caps personalised invitation notes at ~5 per MONTH on a free
    // account; after that the "Add a note" control is simply not offered. A
    // pipeline that requires a note therefore works about five times and then
    // trips the missing-selector breaker every day thereafter. So the invite
    // carries no note, and ALL the personalisation lives in the DM that follows
    // acceptance — where there is no monthly cap and 1900 characters to use.
    // Pass a note explicitly (with --with-notes) only on an account that has them.
    if (withNotes) {
      const noteErrs = validateNote(lead.note);
      if (noteErrs.length) { rejected.push({ lead, reason: noteErrs.join('; ') }); continue; }
    }

    // The DM to send IF they accept. Validated now, while the writer's output is
    // still in front of us, and stored on the invite so the acceptance sweep
    // weeks later has it. An invite with no follow-up message is pointless: it
    // spends the scarce resource and has nothing to say afterwards.
    const msg = (lead.message ?? '').trim();
    if (!msg) { rejected.push({ lead, reason: 'no follow-up DM composed for this person' }); continue; }

    seen.add(url);
    eligible.push({
      leadId: lead.leadId ?? null,
      name: lead.name ?? null,
      company: lead.company ?? null,
      companyDomain: lead.companyDomain ?? null,
      title: lead.title ?? null,
      email: lead.email ?? null,
      score: Number(lead.score ?? 0),
      gap: lead.gap ?? null,
      profileUrl: url,
      note: withNotes ? lead.note.trim() : null,
      message: msg,
      // OPERATOR-CHOSEN TEMPLATE (2026-09-02 directive). The uniqueness check
      // below exists to catch a writer that DRIFTED into a template by
      // accident. A message the operator deliberately fixed, with only the
      // name varying, is not drift, and the flag is set by the renderer
      // (draft_linkedin.py --phase render), never by a writer agent. It is
      // carried into state so generate-dm.js weeks later knows the same.
      templated: lead.templated === true,
    });
  }

  eligible.sort((a, b) => b.score - a.score);

  // Exactly today's ramp cap — no more, and never above the absolute ceiling.
  const selected = eligible.slice(0, Math.min(cap, limits.absoluteDailyMax));

  // Uniqueness is enforced on whatever text actually goes out. With no note on
  // the invite, that is the follow-up DM — checking it here, at queue time,
  // means a templated batch is caught before a single invite is spent on it.
  const free = selected.filter((l) => !l.templated);
  const checked = withNotes ? free.map((l) => l.note) : free.map((l) => l.message);
  const dupes = findNearDuplicates(checked);
  if (dupes.length) {
    const err = new Error(
      `${withNotes ? 'notes' : 'follow-up messages'} are not materially different: ${dupes
        .map((d) => `#${d.i}~#${d.j} (jaccard ${d.similarity}, shared opening ${d.sharedOpeningWords}w)`)
        .join(', ')}`,
    );
    err.nearDuplicates = dupes;
    throw err;
  }

  const schedule = buildSchedule(limits, selected.length, now, rng);

  return {
    generatedAt: now.toISOString(),
    date: dateKeyInTz(now, limits.timezone),
    timezone: limits.timezone,
    campaignDay: day,
    cap,
    entries: selected.map((lead, i) => ({
      ...lead,
      scheduledAt: schedule[i].toISOString(),
      channel: 'linkedin-invite',
    })),
    rejected,
  };
}

async function main() {
  const argv = process.argv.slice(2);
  const leadsFileIdx = argv.indexOf('--leads-file');
  const limits = loadLimits();
  const now = new Date();

  if (!isSendDay(limits, now) && !argv.includes('--force')) {
    console.log(`[generate] ${dateKeyInTz(now, limits.timezone)} is not a send day — writing an empty queue.`);
  }

  const state = loadState(paths.state);
  const suppression = loadSuppression([paths.suppression, paths.bounces]);

  // --from-backlog is the DAILY command. The fire ranks weeks of invite supply
  // in one go; this drains it a ramp-cap at a time without ever having to name
  // the run folder that produced it. Leads already acted on are filtered out by
  // the state check inside generateQueue, so re-running it is safe.
  const leads = argv.includes('--from-backlog')
    ? JSON.parse(fs.readFileSync(paths.backlog, 'utf8'))
    : leadsFileIdx !== -1
      ? JSON.parse(fs.readFileSync(argv[leadsFileIdx + 1], 'utf8'))
      : claudeLeadSource(path.join(paths.root, 'prompts', 'generate.md'));

  const sendDay = isSendDay(limits, now) || argv.includes('--force');
  const withNotes = argv.includes('--with-notes');
  let queue;
  try {
    queue = sendDay
      ? generateQueue({ limits, state, suppression, leads, now, withNotes })
      : { generatedAt: now.toISOString(), date: dateKeyInTz(now, limits.timezone), timezone: limits.timezone, campaignDay: campaignDay(state, limits.timezone, now), cap: 0, entries: [], rejected: [] };
  } catch (e) {
    alert(paths.alerts, 'ERROR', `queue generation failed: ${e.message}`, { nearDuplicates: e.nearDuplicates });
    process.exit(1);
  }

  fs.mkdirSync(path.dirname(paths.queue), { recursive: true });
  fs.writeFileSync(paths.queue, JSON.stringify(queue, null, 2));
  console.log(
    `[generate] day ${queue.campaignDay}, cap ${queue.cap}, queued ${queue.entries.length}, rejected ${queue.rejected.length} -> ${paths.queue}`,
  );
}

if (import.meta.url === `file://${process.argv[1]}`) main();

#!/usr/bin/env node
/*
 * THE JOURNEY TICK — the scheduler half of the lead journey.
 *
 * It decides WHO is due for WHAT, today. It sends nothing, opens no browser and
 * calls no model: it writes a leads-file for queue/generate-dm.js (which
 * re-checks every gate) and a notify queue for the human step. messenger.js
 * remains the only thing that can put a message on LinkedIn, and limits.json
 * remains the only thing that decides how many go.
 *
 * WHY IT IS SAFE TO RUN ON A SCHEDULE
 * Due dates are derived from stamps, not stored, so a tick is idempotent and a
 * missed tick is not a lost step — it is an overdue one, served on the next run.
 * That is what lets this live in a cloud routine that may fire late or twice.
 *
 *   1. enter   — accepted / directly-messageable leads join the journey
 *   2. due     — lib/journey.js evaluates every lead against config/journey.json
 *   3. emit    — dm steps -> state/journey-leads.json, notify steps -> state/notify-queue.json
 *   4. reconcile — steps whose DM messenger.js confirmed get stamped
 *
 * USAGE  node scripts/journey_tick.js               # DRY RUN — prints the plan
 *        node scripts/journey_tick.js --commit      # write queue + enter leads
 *        node scripts/journey_tick.js --reconcile   # stamp steps that were sent
 *        node scripts/journey_tick.js --mark-replied <profileUrl>
 */
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { paths } from '../lib/paths.js';
import { loadState, saveState, normalizeProfileUrl } from '../lib/state.js';
import { loadSuppression } from '../lib/suppression.js';
import { alert } from '../lib/alerts.js';
import { loadJourney, dueNow, evaluate, enterJourney, completeStep, markReplied, journeyPath } from '../lib/journey.js';

const JOURNEY_FILE = process.env.LINKEDIN_JOURNEY_FILE || path.join(paths.root, 'config', 'journey.json');
const STATE_DIR = path.dirname(paths.state);
export const JOURNEY_LEADS = path.join(STATE_DIR, 'journey-leads.json');
export const NOTIFY_QUEUE = path.join(STATE_DIR, 'notify-queue.json');

/**
 * Who belongs in the journey at all.
 *
 * The consent gate is unchanged and non-negotiable: an accepted invite, or a
 * lead independently flagged messageable. Being due for a step never creates
 * permission to send — it only asks the question.
 */
export function shouldEnter(lead) {
  if (lead.journey?.entryAt) return false;
  if (lead.dmStatus === 'not-messageable') return false;
  const accepted = lead.status === 'accepted' || lead.accepted === true || lead.degree === 1;
  const direct = lead.directMessageable === true || lead.openProfile === true;
  return accepted || direct;
}

/**
 * The text a step sends. `outreach` uses the DM the li-writer composed for this
 * person at fire time and sender.js stored on the lead. `video` is one fixed
 * operator message for everyone — David's words, only the name varies — so it
 * is marked templated and exempt from the near-duplicate check that exists to
 * catch lazily-varied *personal* messages.
 */
export function messageForStep({ lead, step, journey, videoText }) {
  if (step.template === 'outreach') {
    return lead.message ? { message: lead.message, templated: lead.templated === true } : null;
  }
  if (step.template === 'video') {
    if (!videoText) return null;         // no text yet => refuse, never send a stub
    const first = (lead.name || '').trim().split(/\s+/)[0] || 'there';
    return { message: videoText.replace(/\{name\}/g, first), templated: true };
  }
  return null;
}

/** config/journey-video.md, minus its comment header. Null until David supplies it. */
export function loadVideoText(file = path.join(paths.root, 'config', 'journey-video.md')) {
  if (!fs.existsSync(file)) return null;
  const body = fs.readFileSync(file, 'utf8')
    .split('\n').filter((l) => !l.startsWith('<!--') && !l.startsWith('#')).join('\n').trim();
  return body || null;
}

export function planTick({ state, journey, now = Date.now(), isSuppressed = () => false, videoText = null }) {
  const leads = Object.values(state.leads || {});
  const entering = leads.filter(shouldEnter);
  const walking = leads.filter((l) => l.journey?.entryAt || shouldEnter(l));
  // Entry is evaluated in-memory so a lead entering today can also be due today
  // (a direct lead with waitDays 0, or an acceptance that is already old).
  for (const l of entering) enterJourney(l, { at: new Date(now) });

  const due = dueNow(walking, journey, { now, isSuppressed });
  const dms = [];
  const notifies = [];
  const blocked = [];

  for (const { lead, step, dueAt } of due) {
    if (step.action === 'notify') { notifies.push({ lead, step, dueAt }); continue; }
    const msg = messageForStep({ lead, step, journey, videoText });
    if (!msg) {
      blocked.push({ lead, step, reason: step.template === 'video'
        ? 'no video message configured (config/journey-video.md)'
        : 'lead carries no composed DM' });
      continue;
    }
    dms.push({ lead, step, dueAt, ...msg });
  }
  return { entering, due, dms, notifies, blocked };
}

function toLeadsFile(dms) {
  return dms.map(({ lead, step, message, templated }) => ({
    leadId: lead.leadId ?? null,
    name: lead.name ?? null,
    company: lead.company ?? null,
    title: lead.title ?? null,
    score: lead.score ?? 0,
    linkedinUrl: lead.profileUrl,
    message,
    templated,
    journeyStep: step.id,
    directMessageable: true,   // already gated by shouldEnter; generate-dm re-checks
  }));
}

/**
 * Stamp the steps whose DM actually went out.
 *
 * Deliberately AFTER the send, keyed off messenger.js's own confirmation stamp:
 * a step marked complete at queue time would be skipped forever if the send
 * failed, silently dropping someone out of the sequence.
 */
export function reconcile(state, journey, now = new Date()) {
  const stamped = [];
  for (const lead of Object.values(state.leads || {})) {
    const pending = lead.journey?.pending;
    if (!pending) continue;
    const sentAt = Date.parse(lead.dmSentAt || '');
    if (lead.dmStatus === 'sent' && Number.isFinite(sentAt) && sentAt >= Date.parse(pending.queuedAt)) {
      completeStep(lead, journey, pending.stepId, new Date(sentAt));
      delete lead.journey.pending;
      stamped.push({ profileUrl: lead.profileUrl, step: pending.stepId });
    } else if (lead.dmStatus === 'not-messageable') {
      lead.journey.status = 'stopped';
      lead.journey.stoppedReason = 'notMessageable';
      delete lead.journey.pending;
    }
  }
  return stamped;
}

function appendNotify(entries) {
  const existing = fs.existsSync(NOTIFY_QUEUE) ? JSON.parse(fs.readFileSync(NOTIFY_QUEUE, 'utf8')) : [];
  const seen = new Set(existing.map((e) => `${e.profileUrl}|${e.step}`));
  const added = entries.filter((e) => !seen.has(`${e.profileUrl}|${e.step}`));
  fs.mkdirSync(path.dirname(NOTIFY_QUEUE), { recursive: true });
  fs.writeFileSync(NOTIFY_QUEUE, JSON.stringify([...existing, ...added], null, 2));
  return added;
}

function main() {
  const argv = process.argv.slice(2);
  const commit = argv.includes('--commit');
  const journey = loadJourney(fs.readFileSync(JOURNEY_FILE, 'utf8'));
  const state = loadState(paths.state);

  const replyIdx = argv.indexOf('--mark-replied');
  if (replyIdx !== -1) {
    const url = normalizeProfileUrl(argv[replyIdx + 1]);
    if (!url || !state.leads[url]) { console.error(`unknown profile: ${argv[replyIdx + 1]}`); process.exit(2); }
    markReplied(state.leads[url]);
    saveState(paths.state, state);
    console.log(`[journey] ${url} marked replied — journey stopped`);
    return;
  }

  if (argv.includes('--reconcile')) {
    const stamped = reconcile(state, journey);
    if (commit || argv.includes('--reconcile')) saveState(paths.state, state);
    console.log(`[journey] reconciled ${stamped.length} step(s): ${stamped.map((s) => `${s.step}`).join(', ') || '—'}`);
    return;
  }

  const suppression = loadSuppression([paths.suppression, paths.bounces]);
  const videoText = loadVideoText();
  const plan = planTick({
    state, journey, now: Date.now(), videoText,
    isSuppressed: (l) => Boolean(suppression.blocks({ ...l, profileUrl: l.profileUrl })),
  });

  console.log(`[journey] ${plan.entering.length} entering, ${plan.dms.length} DM step(s) due, ` +
              `${plan.notifies.length} to notify, ${plan.blocked.length} blocked`);
  for (const d of plan.dms) console.log(`  DM   ${d.step.id.padEnd(9)} ${d.lead.name ?? d.lead.profileUrl} (due ${d.dueAt})`);
  for (const n of plan.notifies) console.log(`  NOTE ${n.step.id.padEnd(9)} ${n.lead.name ?? n.lead.profileUrl} — no reply to the video`);
  for (const b of plan.blocked) console.log(`  SKIP ${b.step.id.padEnd(9)} ${b.lead.name ?? b.lead.profileUrl} — ${b.reason}`);

  if (!commit) { console.log('   (dry run — pass --commit to queue)'); return; }

  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.writeFileSync(JOURNEY_LEADS, JSON.stringify(toLeadsFile(plan.dms), null, 2));
  const queuedAt = new Date().toISOString();
  for (const d of plan.dms) {
    state.leads[d.lead.profileUrl] = d.lead;
    d.lead.journey.pending = { stepId: d.step.id, queuedAt };
  }
  for (const n of plan.notifies) {
    state.leads[n.lead.profileUrl] = n.lead;
    completeStep(n.lead, journey, n.step.id);   // the notification IS the action
  }
  for (const l of plan.entering) state.leads[l.profileUrl] = l;

  const added = appendNotify(plan.notifies.map((n) => ({
    profileUrl: n.lead.profileUrl, name: n.lead.name ?? null, company: n.lead.company ?? null,
    step: n.step.id, dueAt: n.dueAt, raisedAt: queuedAt,
    reason: 'no reply to the outreach or the video — needs a human decision',
  })));
  if (added.length) alert(paths.alerts, 'INFO', `${added.length} lead(s) went cold and need David`, { leads: added.map((a) => a.profileUrl) });

  saveState(paths.state, state);
  console.log(`[journey] wrote ${plan.dms.length} lead(s) to ${JOURNEY_LEADS}`);
}

// pathToFileURL, not `file://${argv[1]}`: this checkout lives in a directory
// with a space in its name, where the naive form never matches and the script
// silently exits 0 having done nothing.
if (import.meta.url === pathToFileURL(process.argv[1]).href) main();

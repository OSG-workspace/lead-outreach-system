import fs from 'node:fs';
import path from 'node:path';
import { dateKeyInTz } from './limits.js';

/*
 * WHY A JSON FILE AND NOT SQLITE
 * ------------------------------
 * The write pattern here is: one process, one action at a time, at most ~20
 * writes a day, each a whole-object rewrite. There is no concurrency (the
 * lockfile guarantees a single sender) and no query load worth an index. A JSON
 * file is greppable, diffable, hand-fixable at 2am, and has no native binary to
 * rebuild. The one thing SQLite would buy — crash-atomic durability — we get
 * from an fsync'd write-to-temp + rename, which is atomic on POSIX. If this ever
 * grows to multiple senders or six-figure rows, that is the moment to switch.
 */

const EMPTY = {
  leads: {},         // profileUrl -> { status, updatedAt, ... }
  days: {},          // YYYY-MM-DD -> { invites: n, dms: n }
  cooldownUntil: null,
  invitesPaused: false,
  lastAlert: null,
};

export function loadState(file) {
  if (!fs.existsSync(file)) return structuredClone(EMPTY);
  const s = JSON.parse(fs.readFileSync(file, 'utf8'));
  return { ...structuredClone(EMPTY), ...s };
}

export function saveState(file, state) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp`;
  const fd = fs.openSync(tmp, 'w');
  fs.writeSync(fd, JSON.stringify(state, null, 2));
  fs.fsyncSync(fd);
  fs.closeSync(fd);
  fs.renameSync(tmp, file);
}

export function normalizeProfileUrl(url) {
  if (!url) return null;
  let u;
  try {
    u = new URL(url.trim());
  } catch {
    return null;
  }
  const host = u.hostname.replace(/^www\./, '').toLowerCase();
  if (!host.endsWith('linkedin.com')) return null;
  const m = /\/in\/([^/?#]+)/i.exec(u.pathname);
  if (!m) return null;
  return `https://www.linkedin.com/in/${decodeURIComponent(m[1]).toLowerCase()}/`;
}

/** Written BEFORE the click, with a timestamp. Crash recovery keys off this. */
export function markAttempting(state, profileUrl, extra = {}) {
  state.leads[profileUrl] = {
    ...(state.leads[profileUrl] || {}),
    ...extra,
    profileUrl,
    status: 'attempting',
    attemptStartedAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  };
}

export function markSent(state, profileUrl, timezone, at = new Date()) {
  const lead = state.leads[profileUrl] || { profileUrl };
  lead.status = 'sent';
  lead.sentAt = at.toISOString();
  lead.updatedAt = at.toISOString();
  delete lead.attemptStartedAt;
  state.leads[profileUrl] = lead;
  const key = dateKeyInTz(at, timezone);
  state.days[key] = state.days[key] || { invites: 0, dms: 0 };
  state.days[key].invites += 1;
}

export function markStatus(state, profileUrl, status, extra = {}) {
  state.leads[profileUrl] = {
    ...(state.leads[profileUrl] || { profileUrl }),
    ...extra,
    status,
    updatedAt: new Date().toISOString(),
  };
}

/**
 * Any "attempting" older than staleMs is UNKNOWN — the invite may have landed.
 * Moved to review-required.json and never auto-retried.
 */
export function reconcileAttempting(state, reviewFile, staleMs = 5 * 60 * 1000, now = Date.now()) {
  const moved = [];
  for (const [url, lead] of Object.entries(state.leads)) {
    if (lead.status !== 'attempting') continue;
    const started = Date.parse(lead.attemptStartedAt || 0);
    if (!Number.isFinite(started) || now - started < staleMs) continue;
    lead.status = 'unknown';
    lead.reason = 'attempt did not confirm; invite may or may not have been sent';
    lead.updatedAt = new Date(now).toISOString();
    delete lead.attemptStartedAt;
    moved.push(lead);
  }
  if (moved.length) appendReview(reviewFile, moved);
  return moved;
}

export function appendReview(reviewFile, entries) {
  fs.mkdirSync(path.dirname(reviewFile), { recursive: true });
  const existing = fs.existsSync(reviewFile) ? JSON.parse(fs.readFileSync(reviewFile, 'utf8')) : [];
  existing.push(...entries.map((e) => ({ ...e, flaggedAt: new Date().toISOString() })));
  fs.writeFileSync(reviewFile, JSON.stringify(existing, null, 2));
}

export function invitesToday(state, timezone, now = new Date()) {
  return state.days[dateKeyInTz(now, timezone)]?.invites || 0;
}

export function invitesInRollingWeek(state, timezone, now = new Date()) {
  let total = 0;
  for (let i = 0; i < 7; i++) {
    const key = dateKeyInTz(new Date(now.getTime() - i * 86400000), timezone);
    total += state.days[key]?.invites || 0;
  }
  return total;
}

/** Campaign day = number of distinct days that have sent, +1 for today if it hasn't. */
export function campaignDay(state, timezone, now = new Date()) {
  const today = dateKeyInTz(now, timezone);
  const sendingDays = Object.entries(state.days).filter(([, v]) => (v.invites || 0) > 0).map(([k]) => k);
  return sendingDays.includes(today) ? sendingDays.length : sendingDays.length + 1;
}

/** DMs sent on the local date of `now` — messenger.js stamps dmSentDay. */
export function dmsToday(state, timezone, now = new Date()) {
  const key = dateKeyInTz(now, timezone);
  return Object.values(state.leads || {}).filter((l) => l.dmSentDay === key).length;
}

/** DMs sent in the rolling 7 days ending today. */
export function dmsInRollingWeek(state, timezone, now = new Date()) {
  const keys = new Set();
  for (let i = 0; i < 7; i++) {
    keys.add(dateKeyInTz(new Date(now.getTime() - i * 86400000), timezone));
  }
  return Object.values(state.leads || {}).filter((l) => keys.has(l.dmSentDay)).length;
}

/**
 * Invites sent and still awaiting an answer.
 *
 * This is the number LinkedIn reacts badly to: a large unanswered pending pile
 * reads as indiscriminate mass-inviting. It also silently poisons
 * trailingAcceptance — every one counts as a non-acceptance — so an unmanaged
 * pile eventually trips the 25% floor and halts sending for a bookkeeping
 * reason rather than a real one.
 */
export function outstandingInvites(state) {
  return Object.values(state.leads || {}).filter((l) => l.status === 'sent').length;
}

/** Leads whose invite has been pending longer than `days`. */
export function stalePending(state, days, now = Date.now()) {
  const cutoff = now - days * 86400000;
  return Object.values(state.leads || {}).filter((l) => {
    if (l.status !== 'sent' || !l.sentAt) return false;
    const t = Date.parse(l.sentAt);
    return Number.isFinite(t) && t < cutoff;
  });
}

/** Acceptance rate over the trailing N invites that have a known outcome. */
export function trailingAcceptance(state, sampleSize) {
  const sent = Object.values(state.leads)
    .filter((l) => l.sentAt && (l.status === 'sent' || l.status === 'accepted' || l.status === 'withdrawn'))
    .sort((a, b) => Date.parse(b.sentAt) - Date.parse(a.sentAt))
    .slice(0, sampleSize);
  if (sent.length < sampleSize) return { rate: null, sample: sent.length };
  const accepted = sent.filter((l) => l.status === 'accepted').length;
  return { rate: (accepted / sent.length) * 100, sample: sent.length };
}

import fs from 'node:fs';
import { paths } from './paths.js';

export class LimitsError extends Error {}

/**
 * Load and validate limits. Throws LimitsError on any violation — sender.js
 * calls this before it does anything else, so a bad config cannot start a run.
 */
export function loadLimits(file = paths.limits) {
  const raw = JSON.parse(fs.readFileSync(file, 'utf8'));
  return validateLimits(raw);
}

/**
 * Shared shape check for a ramp array. Invites and DMs use the same form, and
 * both must be bounded by their own absolute ceiling — a ramp that can climb
 * past the ceiling is how an account gets restricted.
 */
function validateRamp(ramp, label, key, ceiling, ceilingName) {
  if (!Array.isArray(ramp) || ramp.length === 0) {
    throw new LimitsError(`${label} must be a non-empty array`);
  }
  let prev = 0;
  for (const step of ramp) {
    const v = step[key];
    if (typeof v !== 'number' || v < 0) {
      throw new LimitsError(`${label} step ${key} must be a non-negative number`);
    }
    // THE guard: no ramp step may ever exceed the absolute ceiling.
    if (v > ceiling) {
      throw new LimitsError(`${label} step ${key}=${v} exceeds ${ceilingName}=${ceiling}`);
    }
    if (step.throughDay !== null) {
      if (typeof step.throughDay !== 'number' || step.throughDay <= prev) {
        throw new LimitsError(`${label} throughDay values must ascend, with a final null step`);
      }
      prev = step.throughDay;
    }
  }
  if (ramp[ramp.length - 1].throughDay !== null) {
    throw new LimitsError(`final ${label} step must have throughDay: null`);
  }
}

export function validateLimits(l) {
  const req = [
    'timezone', 'ramp', 'absoluteDailyMax', 'weeklyMax', 'followUpDmsPerDay',
    'sendWindow', 'sendDays', 'gapMinutes', 'burstPause', 'profileDwellSeconds',
    'withdrawPendingAfterDays', 'acceptanceFloorPct', 'acceptanceFloorSampleSize',
    // Added 2026-08-03. DMs previously had NO ramp and NO weekly ceiling (flat
    // 25/day from a cold account = up to 125/week), and nothing capped the
    // outstanding-invite pile. Both are well-known restriction triggers.
    'dmRamp', 'dmWeeklyMax', 'maxOutstandingInvites',
  ];
  for (const k of req) {
    if (l[k] === undefined || l[k] === null) throw new LimitsError(`limits.json missing "${k}"`);
  }
  validateRamp(l.ramp, 'ramp', 'invitesPerDay', l.absoluteDailyMax, 'absoluteDailyMax');
  validateRamp(l.dmRamp, 'dmRamp', 'dmsPerDay', l.followUpDmsPerDay, 'followUpDmsPerDay');

  if (l.absoluteDailyMax > l.weeklyMax) throw new LimitsError('absoluteDailyMax exceeds weeklyMax');
  if (l.followUpDmsPerDay > l.dmWeeklyMax) throw new LimitsError('followUpDmsPerDay exceeds dmWeeklyMax');
  if (!(l.maxOutstandingInvites > 0)) throw new LimitsError('maxOutstandingInvites must be positive');
  if (l.gapMinutes.min > l.gapMinutes.max) throw new LimitsError('gapMinutes.min > gapMinutes.max');
  if (minutesOfDay(l.sendWindow.start) >= minutesOfDay(l.sendWindow.end)) {
    throw new LimitsError('sendWindow.start must precede sendWindow.end');
  }
  return l;
}

/** Cap for campaign day N (1-indexed). Boundaries inclusive: throughDay 5 covers day 5. */
export function capForDay(limits, day) {
  if (!Number.isInteger(day) || day < 1) throw new LimitsError(`invalid campaign day: ${day}`);
  for (const step of limits.ramp) {
    if (step.throughDay === null || day <= step.throughDay) {
      return Math.min(step.invitesPerDay, limits.absoluteDailyMax);
    }
  }
  return 0;
}

/**
 * DM cap for campaign day N. Same shape as capForDay.
 *
 * DMs only ever go to people who already accepted an invite, so they are much
 * safer than invites — but "safer" is not "unlimited", and this used to be a
 * flat 25/day from day one with no weekly ceiling at all. A cold account
 * sending 125 messages in its first week looks like exactly what it is.
 */
export function dmCapForDay(limits, day) {
  if (!Number.isInteger(day) || day < 1) throw new LimitsError(`invalid campaign day: ${day}`);
  for (const step of limits.dmRamp) {
    if (step.throughDay === null || day <= step.throughDay) {
      return Math.min(step.dmsPerDay, limits.followUpDmsPerDay);
    }
  }
  return 0;
}

export function minutesOfDay(hhmm) {
  const m = /^(\d{2}):(\d{2})$/.exec(hhmm);
  if (!m) throw new LimitsError(`bad time "${hhmm}", expected HH:MM`);
  return Number(m[1]) * 60 + Number(m[2]);
}

const DAY_KEYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];

/** Day-of-week key ("mon") for a Date in the configured timezone. */
export function dayKeyInTz(date, timezone) {
  const name = new Intl.DateTimeFormat('en-US', { timeZone: timezone, weekday: 'short' })
    .format(date).toLowerCase();
  return DAY_KEYS.includes(name) ? name : name.slice(0, 3);
}

/** YYYY-MM-DD for a Date in the configured timezone. */
export function dateKeyInTz(date, timezone) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(date);
}

export function isSendDay(limits, date) {
  return limits.sendDays.includes(dayKeyInTz(date, limits.timezone));
}

/** Minutes past local midnight for a Date, in the configured timezone. */
export function minutesOfDayInTz(date, timezone) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: timezone, hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(date);
  const h = Number(parts.find((p) => p.type === 'hour').value);
  const m = Number(parts.find((p) => p.type === 'minute').value);
  return h * 60 + m;
}

export function isInSendWindow(limits, date) {
  const mins = minutesOfDayInTz(date, limits.timezone);
  return mins >= minutesOfDay(limits.sendWindow.start) && mins <= minutesOfDay(limits.sendWindow.end);
}

/**
 * Absolute Date for HH:MM-of-day `mins` on the local date of `ref`, in tz.
 * Resolves via a UTC probe + offset correction (no external tz library).
 */
export function dateAtMinutesInTz(ref, timezone, mins) {
  const dateKey = dateKeyInTz(ref, timezone);
  const [y, mo, d] = dateKey.split('-').map(Number);
  let guess = Date.UTC(y, mo - 1, d, Math.floor(mins / 60), mins % 60, 0, 0);
  for (let i = 0; i < 3; i++) {
    const actual = minutesOfDayInTz(new Date(guess), timezone);
    const drift = mins - actual;
    if (drift === 0) break;
    guess += drift * 60000;
  }
  return new Date(guess);
}

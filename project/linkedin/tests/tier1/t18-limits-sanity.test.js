import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { validateLimits, capForDay, dmCapForDay, LimitsError } from '../../lib/limits.js';
import { outstandingInvites, stalePending, dmsToday, dmsInRollingWeek } from '../../lib/state.js';
import { paths } from '../../lib/paths.js';

const L = JSON.parse(fs.readFileSync(paths.limits, 'utf8'));

/*
 * T18 — the anti-ban ceilings, and that they are REAL rather than decorative.
 *
 * The whole point of this channel's config is that a bad queue can only ever be
 * truncated, never obeyed. Three gaps found on 2026-08-03:
 *
 *   - `withdrawPendingAfterDays` was read by NOTHING except the validator's own
 *     required-keys list. Nothing withdrew anything, so pending invites piled up
 *     forever — a mass-inviting signal to LinkedIn, and a slow poisoning of
 *     trailingAcceptance until the 25% floor halted sending for a bookkeeping
 *     reason rather than a real one.
 *   - DMs had NO ramp and NO weekly ceiling: a flat 25/day from a cold account,
 *     i.e. up to 125 messages in week one.
 *   - Nothing capped the outstanding-invite pile at all.
 */

test('T18 the shipped limits stay under LinkedIn free-account reality', () => {
  assert.doesNotThrow(() => validateLimits(structuredClone(L)));
  // ~100 invites/week is the free-account ceiling that actually gets accounts
  // restricted. Stay strictly under it, with headroom.
  assert.ok(L.weeklyMax <= 95, `weeklyMax ${L.weeklyMax} is too close to LinkedIn's ~100/week`);
  assert.ok(L.absoluteDailyMax <= 25, `absoluteDailyMax ${L.absoluteDailyMax} too high`);
  // The ramp must actually start low — a cold account opening at its cap is the
  // loudest automation signal there is.
  assert.ok(L.ramp[0].invitesPerDay <= 10, 'the ramp must start at <=10/day');
  // 5 send days * the top rate must not exceed the weekly ceiling.
  const top = Math.max(...L.ramp.map((s) => s.invitesPerDay));
  assert.ok(top * L.sendDays.length <= L.weeklyMax,
    `${top}/day x ${L.sendDays.length} days exceeds weeklyMax ${L.weeklyMax}`);
});

test('T18 DMs are ramped and weekly-capped, not flat-and-unbounded', () => {
  assert.ok(Array.isArray(L.dmRamp) && L.dmRamp.length > 0, 'dmRamp missing');
  assert.ok(L.dmRamp[0].dmsPerDay <= 10, 'the DM ramp must start low too');
  assert.ok(L.dmWeeklyMax > 0 && L.dmWeeklyMax <= 120, `dmWeeklyMax ${L.dmWeeklyMax} unreasonable`);
  const top = Math.max(...L.dmRamp.map((s) => s.dmsPerDay));
  assert.ok(top * L.sendDays.length <= L.dmWeeklyMax + top,
    'the DM ramp can outrun dmWeeklyMax');
  // Boundaries, same contract as capForDay.
  assert.equal(dmCapForDay(L, 1), L.dmRamp[0].dmsPerDay);
  assert.ok(dmCapForDay(L, 400) <= L.followUpDmsPerDay);
  assert.throws(() => dmCapForDay(L, 0), LimitsError);
  for (let d = 1; d <= 60; d++) assert.ok(dmCapForDay(L, d) <= L.followUpDmsPerDay);
});

test('T18 a dmRamp above followUpDmsPerDay is REFUSED, like the invite ramp', () => {
  const bad = structuredClone(L);
  bad.dmRamp[bad.dmRamp.length - 1].dmsPerDay = bad.followUpDmsPerDay + 1;
  assert.throws(() => validateLimits(bad), LimitsError);
});

test('T18 the pending-pile ceiling exists and is enforced in sender.js', () => {
  assert.ok(L.maxOutstandingInvites > 0);
  const src = fs.readFileSync(path.join(paths.root, 'send/sender.js'), 'utf8');
  assert.match(src, /maxOutstandingInvites/, 'sender.js does not enforce maxOutstandingInvites');
  assert.match(src, /outstandingInvites\(state\)/);
});

test('T18 withdrawPendingAfterDays is no longer dead config', () => {
  // It used to appear ONLY in the validator's required-keys list.
  const withdrawer = path.join(paths.root, 'scripts/withdraw_pending.js');
  assert.ok(fs.existsSync(withdrawer), 'scripts/withdraw_pending.js is missing');
  assert.match(fs.readFileSync(withdrawer, 'utf8'), /withdrawPendingAfterDays/);
  // ...and the daily drip must actually run it, or it is still decorative.
  assert.match(fs.readFileSync(path.join(paths.root, 'scripts/daily.sh'), 'utf8'),
    /withdraw_pending\.js/, 'daily.sh never runs the withdrawer');
});

test('T18 stalePending picks exactly the invites older than the window', () => {
  const now = Date.parse('2026-08-03T12:00:00Z');
  const day = 86400000;
  const state = { leads: {
    fresh:    { status: 'sent', sentAt: new Date(now - 3 * day).toISOString(), profileUrl: 'a' },
    stale:    { status: 'sent', sentAt: new Date(now - 30 * day).toISOString(), profileUrl: 'b' },
    accepted: { status: 'accepted', sentAt: new Date(now - 90 * day).toISOString(), profileUrl: 'c' },
    noDate:   { status: 'sent', profileUrl: 'd' },
  } };
  const got = stalePending(state, 21, now).map((l) => l.profileUrl);
  assert.deepEqual(got, ['b'], 'must pick only the long-pending SENT invite');
  assert.equal(outstandingInvites(state), 3);   // the three still 'sent'
});

test('T18 DM day/week counters read the lead records messenger.js stamps', () => {
  const state = { leads: {
    a: { dmSentDay: '2026-08-03' },
    b: { dmSentDay: '2026-08-03' },
    c: { dmSentDay: '2026-07-30' },   // inside the rolling week
    d: { dmSentDay: '2026-06-01' },   // outside
  } };
  const now = new Date('2026-08-03T12:00:00Z');
  assert.equal(dmsToday(state, 'UTC', now), 2);
  assert.equal(dmsInRollingWeek(state, 'UTC', now), 3);
});

test('T18 messenger re-derives the DM caps itself, never trusting the queue', () => {
  const src = fs.readFileSync(path.join(paths.root, 'send/messenger.js'), 'utf8');
  assert.match(src, /dmCapForDay\(limits/);
  assert.match(src, /dmWeeklyMax/);
  assert.match(src, /dmsInRollingWeek/);
});

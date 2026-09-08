/*
 * The journey engine. These are the guarantees the schedule rests on:
 * order, derivation from stamps, idempotence, and the reply stop.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadJourney, validateJourney, evaluate, dueNow, completeStep, enterJourney, markReplied } from '../../lib/journey.js';
import { shouldEnter, messageForStep, reconcile, planTick } from '../../scripts/journey_tick.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const journey = loadJourney(fs.readFileSync(path.join(ROOT, 'config', 'journey.json'), 'utf8'));
const DAY = 86400000;
const T0 = Date.parse('2026-09-01T10:00:00Z');

const accepted = (over = {}) => ({
  profileUrl: 'https://www.linkedin.com/in/x/', name: 'Sara Khoury', status: 'accepted',
  acceptedAt: new Date(T0).toISOString(), message: 'a real composed DM', ...over,
});

test('the shipped config is valid', () => {
  assert.deepEqual(validateJourney(journey), []);
});

test('a step may only wait on entry or an earlier step', () => {
  const errs = validateJourney({ steps: [{ id: 'a', action: 'dm', after: { days: 1, from: 'b' } }] });
  assert.ok(errs.some((e) => e.includes('not entry or an earlier step')));
});

test('outreach is due 2 days after acceptance, not before', () => {
  const lead = enterJourney(accepted(), { at: new Date(T0) });
  assert.equal(evaluate(lead, journey, { now: T0 + 1.9 * DAY }).due, false);
  const v = evaluate(lead, journey, { now: T0 + 2 * DAY });
  assert.equal(v.due, true);
  assert.equal(v.step.id, 'outreach');
});

test('a direct lead skips the acceptance wait but walks the same steps after', () => {
  const lead = enterJourney({ profileUrl: 'https://www.linkedin.com/in/y/', directMessageable: true, message: 'hi' },
                            { path: 'direct', at: new Date(T0) });
  assert.equal(evaluate(lead, journey, { now: T0 }).step.id, 'outreach');
  assert.equal(evaluate(lead, journey, { now: T0 }).due, true);
  completeStep(lead, journey, 'outreach', new Date(T0));
  assert.equal(evaluate(lead, journey, { now: T0 + 3 * DAY }).step.id, 'video');
});

test('the video waits on the outreach STAMP, so a late outreach moves it', () => {
  const lead = enterJourney(accepted(), { at: new Date(T0) });
  completeStep(lead, journey, 'outreach', new Date(T0 + 9 * DAY));
  assert.equal(evaluate(lead, journey, { now: T0 + 11 * DAY }).due, false);
  assert.equal(evaluate(lead, journey, { now: T0 + 12 * DAY }).step.id, 'video');
});

test('a reply stops the journey wherever it stands', () => {
  const lead = enterJourney(accepted(), { at: new Date(T0) });
  completeStep(lead, journey, 'outreach', new Date(T0 + 2 * DAY));
  markReplied(lead, new Date(T0 + 3 * DAY));
  assert.deepEqual(evaluate(lead, journey, { now: T0 + 99 * DAY }), { due: false, reason: 'replied' });
});

test('steps never run out of order or twice', () => {
  const lead = enterJourney(accepted(), { at: new Date(T0) });
  const seen = [];
  for (let d = 0; d < 30; d++) {
    const v = evaluate(lead, journey, { now: T0 + d * DAY });
    if (v.due) { seen.push(v.step.id); completeStep(lead, journey, v.step.id, new Date(T0 + d * DAY)); }
  }
  assert.deepEqual(seen, ['outreach', 'video', 'notify']);
  assert.equal(lead.journey.status, 'done');
});

test('a missed tick produces an overdue step, never a lost one', () => {
  const lead = enterJourney(accepted(), { at: new Date(T0) });
  const v = evaluate(lead, journey, { now: T0 + 40 * DAY });   // nothing ran for 40 days
  assert.equal(v.due, true);
  assert.equal(v.step.id, 'outreach');
});

test('entering is idempotent and never resets the clock', () => {
  const lead = enterJourney(accepted(), { at: new Date(T0) });
  enterJourney(lead, { at: new Date(T0 + 5 * DAY) });
  assert.equal(lead.journey.entryAt, new Date(T0).toISOString());
});

test('only accepted or directly-messageable leads enter', () => {
  assert.equal(shouldEnter({ status: 'sent' }), false);
  assert.equal(shouldEnter({ status: 'accepted' }), true);
  assert.equal(shouldEnter({ directMessageable: true }), true);
  assert.equal(shouldEnter({ status: 'accepted', dmStatus: 'not-messageable' }), false);
});

test('the video step refuses to send while no text is configured', () => {
  const step = journey.steps.find((s) => s.id === 'video');
  assert.equal(messageForStep({ lead: accepted(), step, journey, videoText: null }), null);
  const m = messageForStep({ lead: accepted(), step, journey, videoText: 'Hi {name}, quick video:' });
  assert.equal(m.message, 'Hi Sara, quick video:');
  assert.equal(m.templated, true, 'one fixed operator message is exempt from the near-duplicate rule');
});

test('a step is stamped only once the DM is confirmed sent', () => {
  const lead = enterJourney(accepted(), { at: new Date(T0) });
  lead.journey.pending = { stepId: 'outreach', queuedAt: new Date(T0 + 2 * DAY).toISOString() };
  const state = { leads: { [lead.profileUrl]: lead } };

  reconcile(state, journey);                                   // send has not happened
  assert.equal(lead.journey.history.outreach, undefined);

  lead.dmStatus = 'sent';
  lead.dmSentAt = new Date(T0 + 2 * DAY + 3600000).toISOString();
  reconcile(state, journey);
  assert.equal(lead.journey.history.outreach, lead.dmSentAt);
  assert.equal(lead.journey.pending, undefined);
});

test('a suppressed lead is never due', () => {
  const lead = enterJourney(accepted(), { at: new Date(T0) });
  assert.deepEqual(dueNow([lead], journey, { now: T0 + 9 * DAY, isSuppressed: () => true }), []);
});

test('planTick enters, plans, and blocks in one pass', () => {
  const state = { leads: {
    a: accepted({ profileUrl: 'a' }),
    b: accepted({ profileUrl: 'b', message: null }),
    c: { profileUrl: 'c', status: 'sent' },              // invite pending — not in the journey
  } };
  const plan = planTick({ state, journey, now: T0 + 3 * DAY });
  assert.equal(plan.entering.length, 2);
  assert.deepEqual(plan.dms.map((d) => d.lead.profileUrl), ['a']);
  assert.equal(plan.blocked[0].reason, 'lead carries no composed DM');
});

/* ---- the cloud bridge: the digest is all a cloud routine can see ---- */
import { buildDigest, staleness, STALE_HOURS } from '../../scripts/journey_digest.js';
import { coldFromDigest, renderStalled, renderDigest } from '../../scripts/notify_journey.js';

test('the digest carries names and profile URLs but never contact PII or message text', () => {
  const lead = enterJourney(accepted({ email: 'sara@x.com', phone: '+96170123456' }), { at: new Date(T0) });
  const d = buildDigest({ state: { leads: { a: lead } }, journey, now: T0 + 3 * DAY });
  const blob = JSON.stringify(d);
  assert.ok(blob.includes('Sara Khoury'));
  assert.ok(!blob.includes('sara@x.com'), 'no email addresses in a tracked file');
  assert.ok(!blob.includes('96170123456'), 'no phone numbers in a tracked file');
  assert.ok(!blob.includes('a real composed DM'), 'no message text in a tracked file');
});

test('the digest reports what the Mac has not sent yet', () => {
  const lead = enterJourney(accepted(), { at: new Date(T0) });
  const d = buildDigest({ state: { leads: { a: lead } }, journey, now: T0 + 5 * DAY });
  assert.equal(d.overdue.length, 1);
  assert.equal(d.overdue[0].step, 'outreach');
  assert.equal(d.counts.inJourney, 1);
});

test('staleness is what tells the cloud the Mac is off', () => {
  const now = T0 + 10 * DAY;
  assert.equal(staleness({ lastRunAt: new Date(now - 5 * 3600000).toISOString() }, now).stale, false);
  assert.equal(staleness({ lastRunAt: new Date(now - (STALE_HOURS + 1) * 3600000).toISOString() }, now).stale, true);
  assert.equal(staleness({}, now).stale, true, 'a digest that was never written is stale');
});

test('the stalled email names the waiting steps and says nothing is lost', () => {
  const { subject, html } = renderStalled({ hours: 50, overdue: [{ name: 'Sara Khoury', company: 'Cedar', profileUrl: 'https://www.linkedin.com/in/x/', step: 'video', dueAt: '2026-09-05T10:00:00Z' }] });
  assert.match(subject, /stalled/);
  assert.match(html, /Sara Khoury/);
  assert.match(html, /video/);
  assert.match(html, /Nothing is lost/);
});

test('cold leads survive the round trip through the digest', () => {
  const d = buildDigest({ state: { leads: {} }, journey, now: T0,
    notifyQueue: [{ profileUrl: 'https://www.linkedin.com/in/x/', name: 'Sara', dueAt: '2026-09-05', notifiedAt: null },
                  { profileUrl: 'https://www.linkedin.com/in/y/', name: 'Rami', dueAt: '2026-09-05', notifiedAt: '2026-09-06' }] });
  const cold = coldFromDigest(d);
  assert.deepEqual(cold.map((c) => c.name), ['Sara'], 'an already-notified lead is not re-emailed');
  assert.match(renderDigest(cold).html, /Sara/);
});

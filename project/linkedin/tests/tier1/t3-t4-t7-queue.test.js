import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { validateLimits } from '../../lib/limits.js';
import { paths } from '../../lib/paths.js';
import { loadState, markAttempting, reconcileAttempting, markSent } from '../../lib/state.js';
import { loadSuppression } from '../../lib/suppression.js';
import { generateQueue } from '../../queue/generate.js';

const LIMITS = validateLimits(JSON.parse(fs.readFileSync(paths.limits, 'utf8')));
const MONDAY = new Date('2026-07-27T08:00:00Z'); // Monday, inside a send day

function tmpdir() {
  const d = fs.mkdtempSync(path.join(os.tmpdir(), 'li-test-'));
  test.after?.(() => {});
  return d;
}

function lead(i, over = {}) {
  return {
    leadId: `L${i}`,
    name: `Lead ${i}`,
    company: `Co ${i}`,
    email: `lead${i}@example.com`,
    linkedinUrl: `https://www.linkedin.com/in/lead-${i}/`,
    score: 100 - i,
    gap: `gap ${i}`,
    // Deliberately disjoint wording per lead: these fixtures exercise queueing
    // rules, not copy quality. Note uniqueness has its own test (T8).
    note: `Lead ${i}: ${Array.from({ length: 12 }, (_, k) => `term${i}x${k}`).join(' ')}?`,
    // The follow-up DM is mandatory on every invite: the invite goes out bare
    // (free accounts get ~5 notes a MONTH) and this is the message that lands
    // if they accept. Disjoint per lead for the same reason as the note.
    message: `DM ${i}: ${Array.from({ length: 14 }, (_, k) => `msg${i}y${k}`).join(' ')}.`,
    ...over,
  };
}

const emptySuppression = { blocks: () => null };

// T3 — idempotency: crash after "attempting" => no double-send
test('T3 a crashed attempt becomes unknown, lands in review, and is never re-queued', () => {
  const dir = tmpdir();
  const reviewFile = path.join(dir, 'review-required.json');
  const state = loadState(path.join(dir, 'state.json'));

  const url = 'https://www.linkedin.com/in/lead-1/';
  markAttempting(state, url, { leadId: 'L1' });
  assert.equal(state.leads[url].status, 'attempting');
  assert.ok(state.leads[url].attemptStartedAt, 'attempting must carry a timestamp');

  // Simulate the process dying, then restarting 6 minutes later.
  const later = Date.parse(state.leads[url].attemptStartedAt) + 6 * 60 * 1000;
  const moved = reconcileAttempting(state, reviewFile, 5 * 60 * 1000, later);

  assert.equal(moved.length, 1);
  assert.equal(state.leads[url].status, 'unknown');
  const review = JSON.parse(fs.readFileSync(reviewFile, 'utf8'));
  assert.equal(review.length, 1);
  assert.equal(review[0].profileUrl, url);

  // And the generator will not hand it back out.
  const q = generateQueue({
    limits: LIMITS, state, suppression: emptySuppression,
    leads: [lead(1), lead(2)], now: MONDAY,
  });
  assert.deepEqual(q.entries.map((e) => e.profileUrl), ['https://www.linkedin.com/in/lead-2/']);
  assert.ok(q.rejected.some((r) => /already unknown/.test(r.reason)));
});

test('T3 a fresh attempt (< 5min) is left alone, not flipped to unknown', () => {
  const dir = tmpdir();
  const state = loadState(path.join(dir, 'state.json'));
  markAttempting(state, 'https://www.linkedin.com/in/lead-1/');
  const moved = reconcileAttempting(state, path.join(dir, 'review.json'), 5 * 60 * 1000, Date.now() + 60 * 1000);
  assert.equal(moved.length, 0);
  assert.equal(state.leads['https://www.linkedin.com/in/lead-1/'].status, 'attempting');
});

test('T3 an already-sent lead is never re-queued', () => {
  const dir = tmpdir();
  const state = loadState(path.join(dir, 'state.json'));
  markSent(state, 'https://www.linkedin.com/in/lead-1/', LIMITS.timezone, MONDAY);
  const q = generateQueue({ limits: LIMITS, state, suppression: emptySuppression, leads: [lead(1)], now: MONDAY });
  assert.equal(q.entries.length, 0);
});

// T4 — suppression
test('T4 an opted-out lead never appears in queue.json (email OR linkedin url)', () => {
  const dir = tmpdir();
  const supFile = path.join(dir, 'suppression.md');
  fs.writeFileSync(supFile, [
    '# Shared suppression list (email + LinkedIn)',
    '- 2026-07-01 lead2@example.com — replied STOP to an email',
    '- 2026-07-02 https://www.linkedin.com/in/lead-3/ — asked to be left alone on LI',
  ].join('\n'));
  const suppression = loadSuppression([supFile]);
  const state = loadState(path.join(dir, 'state.json'));

  const q = generateQueue({
    limits: LIMITS, state, suppression,
    leads: [lead(1), lead(2), lead(3), lead(4)], now: MONDAY,
  });
  const urls = q.entries.map((e) => e.profileUrl);
  assert.ok(!urls.includes('https://www.linkedin.com/in/lead-2/'), 'email opt-out must block LinkedIn');
  assert.ok(!urls.includes('https://www.linkedin.com/in/lead-3/'), 'LinkedIn opt-out must block LinkedIn');
  assert.equal(urls.length, 2);
  assert.equal(q.rejected.filter((r) => /suppressed/.test(r.reason)).length, 2);
});

test('T4 suppression matching is case- and www-insensitive', () => {
  const dir = tmpdir();
  const supFile = path.join(dir, 'suppression.md');
  fs.writeFileSync(supFile, 'https://linkedin.com/in/Lead-1?trk=xyz\nLEAD2@Example.com\n');
  const suppression = loadSuppression([supFile]);
  assert.ok(suppression.blocks({ linkedinUrl: 'https://www.linkedin.com/in/lead-1/' }));
  assert.ok(suppression.blocks({ email: 'lead2@example.com' }));
});

// T7 — dedup on profile URL, not name
test('T7 the same profile URL twice yields exactly one queue entry', () => {
  const dir = tmpdir();
  const state = loadState(path.join(dir, 'state.json'));
  const dupes = [
    lead(1),
    lead(1, { leadId: 'L1-dup', name: 'Different Name', email: 'other@example.com', linkedinUrl: 'https://linkedin.com/in/Lead-1/?originalSubdomain=fr' }),
  ];
  const q = generateQueue({ limits: LIMITS, state, suppression: emptySuppression, leads: dupes, now: MONDAY });
  assert.equal(q.entries.length, 1);
  assert.ok(q.rejected.some((r) => r.reason === 'duplicate profile URL'));
});

test('T7 two different people with the same name both queue', () => {
  const dir = tmpdir();
  const state = loadState(path.join(dir, 'state.json'));
  const same = [lead(1, { name: 'Jean Dupont' }), lead(2, { name: 'Jean Dupont' })];
  const q = generateQueue({ limits: LIMITS, state, suppression: emptySuppression, leads: same, now: MONDAY });
  assert.equal(q.entries.length, 2);
});

test('queue never exceeds today ramp cap and is ranked by score desc', () => {
  const dir = tmpdir();
  const state = loadState(path.join(dir, 'state.json'));
  const many = Array.from({ length: 40 }, (_, i) => lead(i));
  const q = generateQueue({ limits: LIMITS, state, suppression: emptySuppression, leads: many, now: MONDAY });
  assert.equal(q.cap, 8, 'day 1 cap');
  assert.equal(q.entries.length, 8);
  const scores = q.entries.map((e) => e.score);
  assert.deepEqual(scores, [...scores].sort((a, b) => b - a));
});

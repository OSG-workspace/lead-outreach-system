import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { validateLimits } from '../../lib/limits.js';
import { paths } from '../../lib/paths.js';
import { generateQueue } from '../../queue/generate.js';
import { generateDmQueue, leadsFromState } from '../../queue/generate-dm.js';

const LIMITS = validateLimits(JSON.parse(fs.readFileSync(paths.limits, 'utf8')));
const MONDAY = new Date('2026-07-27T08:00:00Z');
const noSuppression = { blocks: () => null };
const emptyState = () => ({ leads: {}, days: {} });

const TEMPLATE = (name) =>
  `Hi ${name}, one person can send maybe fifty personal messages a day, our system does thousands, ` +
  `only to people who match the criteria you set. Worth ten minutes on a call?`;

const lead = (i, over = {}) => ({
  leadId: `L${i}`, name: `Person ${i}`, company: `Co ${i}`, score: 50 - i,
  linkedinUrl: `https://www.linkedin.com/in/person-${i}/`,
  message: TEMPLATE(`Person ${i}`), ...over,
});

// T19 — the operator chose one fixed DM (directive 2026-09-02). The uniqueness
// breaker exists to catch a WRITER drifting into a template; it must not fire
// on a template the operator set on purpose, and must still fire on drift.

test('T19 an operator template with only the name varying is queued, not rejected as near-duplicate', () => {
  const leads = [1, 2, 3, 4].map((i) => lead(i, { templated: true }));
  const q = generateQueue({ limits: LIMITS, state: emptyState(), suppression: noSuppression, leads, now: MONDAY });
  assert.equal(q.entries.length, 4);
  assert.ok(q.entries.every((e) => e.templated === true), 'the flag must travel with the entry into state');
});

test('T19 the same messages WITHOUT the flag are still caught as writer drift', () => {
  const leads = [1, 2, 3, 4].map((i) => lead(i));
  assert.throws(
    () => generateQueue({ limits: LIMITS, state: emptyState(), suppression: noSuppression, leads, now: MONDAY }),
    /not materially different/,
  );
});

test('T19 the flag is per lead: templated entries never shield an untemplated near-duplicate pair', () => {
  const leads = [lead(1, { templated: true }), lead(2, { templated: true }), lead(3), lead(4)];
  assert.throws(
    () => generateQueue({ limits: LIMITS, state: emptyState(), suppression: noSuppression, leads, now: MONDAY }),
    /not materially different/,
  );
});

test('T19 weeks later the DM queue honours the flag stored on the accepted lead', () => {
  const state = emptyState();
  for (const i of [1, 2, 3]) {
    const url = `https://www.linkedin.com/in/person-${i}/`;
    state.leads[url] = { profileUrl: url, status: 'accepted', name: `Person ${i}`,
                         message: TEMPLATE(`Person ${i}`), templated: true };
  }
  const leads = leadsFromState(state);
  assert.ok(leads.every((l) => l.templated === true));
  const q = generateDmQueue({ limits: LIMITS, state, suppression: noSuppression, leads, now: MONDAY });
  assert.equal(q.entries.length, 3);
  assert.equal(q.rejected.length, 0);
});

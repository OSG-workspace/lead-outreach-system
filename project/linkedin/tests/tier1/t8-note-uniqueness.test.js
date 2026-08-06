import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { validateLimits } from '../../lib/limits.js';
import { paths } from '../../lib/paths.js';
import { loadState } from '../../lib/state.js';
import { generateQueue } from '../../queue/generate.js';
import { findNearDuplicates, validateNote, MAX_NOTE_CHARS } from '../../lib/notes.js';

const LIMITS = validateLimits(JSON.parse(fs.readFileSync(paths.limits, 'utf8')));
const MONDAY = new Date('2026-07-27T08:00:00Z');
const NOTES = JSON.parse(fs.readFileSync(new URL('../fixtures/notes-20.json', import.meta.url), 'utf8'));

// T8 — 20 generated notes, assert no near-duplicates
test('T8 the 20-note fixture has no near-duplicates', () => {
  assert.equal(NOTES.length, 20);
  const dupes = findNearDuplicates(NOTES);
  assert.deepEqual(dupes, [], `near-duplicate notes: ${JSON.stringify(dupes)}`);
});

test('T8 every note is under 280 chars and placeholder-free', () => {
  for (const n of NOTES) {
    assert.deepEqual(validateNote(n), [], `bad note: ${n}`);
    assert.ok(n.length <= MAX_NOTE_CHARS);
  }
});

test('T8 templated notes ARE caught (detector is not a rubber stamp)', () => {
  const templated = [
    'Hi Dana, I saw your team is still booking every appointment by phone and wanted to connect about it.',
    'Hi Marc, I saw your team is still booking every appointment by phone and wanted to connect about it.',
  ];
  assert.equal(findNearDuplicates(templated).length, 1);
});

test('T8 a shared long opening clause is caught even when the tails differ', () => {
  const notes = [
    'Noticed you are still triaging every inbound enquiry by hand at the front desk of the clinic.',
    'Noticed you are still triaging every inbound enquiry by hand, which is unusual for a firm of forty solicitors.',
  ];
  assert.ok(findNearDuplicates(notes).length >= 1);
});

test('T8 the generator refuses to write a queue containing near-duplicate notes', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'li-t8-'));
  const state = loadState(path.join(dir, 'state.json'));
  const shared = 'Saw that every booking at your place still lands in one shared inbox and wondered how you cope with the overflow.';
  const leads = [1, 2].map((i) => ({
    leadId: `L${i}`,
    linkedinUrl: `https://www.linkedin.com/in/dup-${i}/`,
    score: 90 - i,
    gap: 'manual inbox',
    note: shared,
    message: `Distinct follow-up ${i}: ${Array.from({ length: 14 }, (_, k) => `w${i}q${k}`).join(' ')}.`,
  }));
  assert.throws(
    () => generateQueue({ limits: LIMITS, state, suppression: { blocks: () => null }, leads, now: MONDAY, withNotes: true }),
    /not materially different/,
  );
});

// The DEFAULT path carries no note at all, so the thing that must be unique is
// the follow-up DM — it is the only copy the recipient ever reads. Catching it
// here means a templated batch is refused before a single invite is spent.
test('T8 near-duplicate follow-up DMs are refused on the note-less default path', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'li-t8c-'));
  const state = loadState(path.join(dir, 'state.json'));
  const shared = 'Saw that every enquiry at your brokerage still lands in one shared inbox and wondered how the team keeps up with it.';
  const leads = [1, 2].map((i) => ({
    leadId: `M${i}`,
    linkedinUrl: `https://www.linkedin.com/in/dm-dup-${i}/`,
    score: 90 - i,
    message: shared,
  }));
  assert.throws(
    () => generateQueue({ limits: LIMITS, state, suppression: { blocks: () => null }, leads, now: MONDAY }),
    /follow-up messages are not materially different/,
  );
});

// An invite with nothing to say afterwards spends the scarcest resource on the
// channel for no return, so it must never reach the queue.
test('T8 an invite with no follow-up DM is rejected, not sent bare', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'li-t8d-'));
  const state = loadState(path.join(dir, 'state.json'));
  const q = generateQueue({
    limits: LIMITS,
    state,
    suppression: { blocks: () => null },
    leads: [{ linkedinUrl: 'https://www.linkedin.com/in/nodm-1/', score: 5 }],
    now: MONDAY,
  });
  assert.equal(q.entries.length, 0);
  assert.match(q.rejected[0].reason, /no follow-up DM composed/);
});

test('T8 an over-length note is rejected at queue time, not sent', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'li-t8b-'));
  const state = loadState(path.join(dir, 'state.json'));
  const q = generateQueue({
    limits: LIMITS,
    state,
    suppression: { blocks: () => null },
    leads: [{
      linkedinUrl: 'https://www.linkedin.com/in/long-1/', score: 5,
      note: 'x'.repeat(300),
      message: 'A perfectly fine follow-up message that is not what this test is about.',
    }],
    now: MONDAY,
    withNotes: true,
  });
  assert.equal(q.entries.length, 0);
  assert.match(q.rejected[0].reason, /300 chars/);
});

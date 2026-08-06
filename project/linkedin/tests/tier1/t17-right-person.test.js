import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { paths } from '../../lib/paths.js';
import { normalizeName, labelNamesPerson, findControlFor } from '../../lib/target.js';

/*
 * T17 — never click a control belonging to someone else.
 *
 * Measured live on 2026-08-03: ONE profile page matched seven
 * `Invite <Name> to connect` buttons inside <main>, every one of them a member
 * in the "people you may know" rail, none of them the page's owner. The old
 * locator took `.first()`.
 *
 * On any profile whose top card offers Follow rather than Connect — very common
 * — that meant: invite a stranger, watch their button flip to Pending, count it
 * as success, and mark the INTENDED lead as sent. Two people burned per click,
 * the stranger permanently and the real lead silently (marked contacted, so
 * never retried).
 *
 * Structure could not fix this: on the probed page `main h1` did not resolve at
 * all. The label could — LinkedIn writes the member's own name into it.
 */

/** Minimal stand-in for a Playwright locator list. */
function fakeLocator(labels) {
  return {
    count: async () => labels.length,
    nth: (i) => ({ getAttribute: async () => labels[i] }),
  };
}

// The real aria-labels captured from the live probe.
const REAL_PAGE = [
  'Invite Adeeb Zaatar to connect',
  'Invite Marten Hajj to connect',
  'Invite Charbel Bou gharios to connect',
  'Invite Mohamad Atwe to connect',
  'Invite Jouanna safsouf to connect',
  'Invite Salman Abou saif to connect',
];

test('T17 the intended person is picked out of a rail full of other people', async () => {
  const hit = await findControlFor(fakeLocator(REAL_PAGE), 'Mohamad Atwe');
  assert.ok(hit, 'should have found the intended person');
  assert.equal(hit.label, 'Invite Mohamad Atwe to connect');
});

test('T17 a page with NOBODY matching returns null rather than the first stranger', async () => {
  // This is the exact live case: we asked for one person, the page offered six
  // others. The only safe answer is "not found".
  const hit = await findControlFor(fakeLocator(REAL_PAGE), 'William Gates');
  assert.equal(hit, null, 'returned a stranger instead of null — this is the bug');
});

test('T17 a missing/blank expected name never authorises a click', async () => {
  for (const name of [undefined, null, '', '   ']) {
    assert.equal(await findControlFor(fakeLocator(REAL_PAGE), name), null, String(name));
  }
});

test('T17 name matching tolerates case, accents and punctuation', () => {
  // The two person-routes spell names differently: the walk copies LinkedIn's
  // rendering, li-finder takes it off a web page.
  assert.ok(labelNamesPerson('Invite Chloé O’Brien to connect', 'chloe obrien'));
  assert.ok(labelNamesPerson('Invite Chloe OBrien to connect', "Chloé O'Brien"));
  assert.ok(labelNamesPerson('Invite AHMED  NASSER to connect', 'Ahmed Nasser'));
  assert.ok(labelNamesPerson('Invite Jean-Pierre Aoun to connect', 'Jean Pierre Aoun'));
  assert.equal(normalizeName('Chloé O’Brien'), 'chloe obrien');
});

test('T17 a different person with a shared first name is NOT a match', () => {
  assert.equal(labelNamesPerson('Invite Ahmed Khalil to connect', 'Ahmed Nasser'), false);
  assert.equal(labelNamesPerson('Invite Sara Khoury to connect', 'Sara Haddad'), false);
});

test('T17 LinkedIn adding a middle name or suffix still matches', () => {
  assert.ok(labelNamesPerson('Invite Ahmed bin Nasser to connect', 'Ahmed Nasser'));
  assert.ok(labelNamesPerson('Invite Sara Khoury, MBA to connect', 'Sara Khoury'));
});

test('T17 neither sender nor messenger can fall back to .first() on a person control', () => {
  // A regression guard on the source itself: .first() over the invite/message
  // locators is what caused this, and it must not come back.
  for (const rel of ['send/sender.js', 'send/messenger.js']) {
    const src = fs.readFileSync(path.join(paths.root, rel), 'utf8');
    const bad = /SELECTORS\.(connectButton|messageButton|moreConnectItem|moreButton)\s*\)\s*\.first\(\)/.test(src);
    assert.equal(bad, false, `${rel} takes .first() on a per-person control again`);
  }
});

test('T17 both senders pass the expected name into the locator', () => {
  const sender = fs.readFileSync(path.join(paths.root, 'send/sender.js'), 'utf8');
  const messenger = fs.readFileSync(path.join(paths.root, 'send/messenger.js'), 'utf8');
  assert.match(sender, /locateConnect\(page,\s*entry\.name\)/);
  assert.match(messenger, /locateMessage\(page,\s*entry\.name\)/);
});

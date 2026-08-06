import test from 'node:test';
import assert from 'node:assert/strict';
import { whoami, slugFromProfileUrl, assertAccount } from '../../lib/identity.js';

/*
 * T14 — the identity gate, and the false positive it used to produce.
 *
 * `/in/me/` is an alias the SERVER resolves: to the member's own profile when
 * logged in, to `/login` when not. At `domcontentloaded` neither has happened,
 * so reading page.url() straight away returns the alias itself — which matched
 * neither the logged-out patterns nor anything suspicious, and yielded the
 * literal slug "me".
 *
 * Caught live on 2026-08-03: a Chrome parked on linkedin.com/login reported
 * "attached account: me". That is the dangerous direction — preflight passes on
 * a dead session, a fire spends OSM ground it cannot use, `whoami --save` writes
 * expectProfile:"me" and permanently neuters the real account check, and the
 * sender walks into login walls until the challenge breaker trips a cooldown.
 */

/** A page stub whose URL changes after `hops` polls, like a real redirect. */
function fakePage(finalUrl, { hops = 1, startUrl = 'https://www.linkedin.com/in/me/' } = {}) {
  let polls = 0;
  return {
    async goto() {},
    async waitForTimeout() { polls++; },
    url() { return polls >= hops ? finalUrl : startUrl; },
  };
}

test('T14 the "me" alias is never accepted as a profile slug', () => {
  assert.equal(slugFromProfileUrl('https://www.linkedin.com/in/me/'), null);
  assert.equal(slugFromProfileUrl('https://www.linkedin.com/in/me'), null);
  assert.equal(slugFromProfileUrl('https://www.linkedin.com/in/david-geha/'), 'david-geha');
});

test('T14 a session that never resolves the alias is NOT reported as logged in', async () => {
  const page = fakePage('https://www.linkedin.com/in/me/', { hops: 999 });
  assert.equal(await whoami(page, 1200), null);
});

test('T14 a redirect to the login wall reads as logged out', async () => {
  const page = fakePage('https://www.linkedin.com/login/?session_redirect=%2Ffeed%2F');
  assert.equal(await whoami(page, 5000), null);
});

test('T14 an authwall / checkpoint reads as logged out', async () => {
  for (const u of ['https://www.linkedin.com/authwall?trk=x',
                   'https://www.linkedin.com/checkpoint/challenge/']) {
    assert.equal(await whoami(fakePage(u), 5000), null, u);
  }
});

test('T14 a real logged-in session resolves to that member slug', async () => {
  const page = fakePage('https://www.linkedin.com/in/david-geha/', { hops: 2 });
  assert.equal(await whoami(page, 5000), 'david-geha');
});

test('T14 assertAccount refuses a logged-out session instead of sending', async () => {
  const page = fakePage('https://www.linkedin.com/login/');
  await assert.rejects(() => assertAccount(page, 'david-geha'), /NOT logged in/);
});

test('T14 assertAccount refuses the WRONG account', async () => {
  const page = fakePage('https://www.linkedin.com/in/someone-else/');
  await assert.rejects(() => assertAccount(page, 'david-geha'), /WRONG LinkedIn account/);
});

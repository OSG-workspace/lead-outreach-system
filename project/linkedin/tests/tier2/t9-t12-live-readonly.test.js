/*
 * TIER 2 — read-only against LinkedIn, with your real attached Chrome.
 * Nothing here clicks anything. T12 opens ONE profile and stops.
 *
 * Prereqs:
 *   Chrome running with --remote-debugging-port=9222, logged in to LinkedIn.
 *   LINKEDIN_TEST_PROFILE=https://www.linkedin.com/in/<someone>/   (for T12)
 *
 * Run: npm run test:tier2
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { attach, detach, CDP_ENDPOINT } from '../../lib/browser.js';

/*
 * NOTE ON WHY package.json PASSES --test-force-exit FOR THIS SUITE.
 *
 * The CDP socket detach() leaves open is a live libuv handle, so node --test
 * runs every test, prints the results, and then never exits ("Promise
 * resolution is still pending but the event loop has already resolved").
 * `npm run test:tier2` therefore looked like it hung forever — which is why it
 * had never been run, and why the reCAPTCHA false positive that T10 catches
 * (a healthy /feed/ reading as a challenge, then a 48h cooldown) sat
 * undiscovered the whole time this channel existed.
 *
 * `process.on('beforeExit')` does NOT help: beforeExit only fires when the
 * event loop empties, and the open socket is precisely what stops it emptying.
 */
import { SELECTORS } from '../../lib/selectors.js';
import { detectChallenge } from '../../lib/breakers.js';
import { findControlFor } from '../../lib/target.js';
import { paths } from '../../lib/paths.js';

const TEST_PROFILE = process.env.LINKEDIN_TEST_PROFILE;
// Optional: the display name of whoever TEST_PROFILE belongs to, so the probe
// can also assert the POSITIVE case (we find exactly them).
const TEST_NAME = process.env.LINKEDIN_TEST_NAME;

async function withPage(fn) {
  const { browser, context } = await attach();
  const page = await context.newPage();
  try {
    return await fn(page, { browser, context });
  } finally {
    await page.close().catch(() => {});   // never browser.close()
    await detach(browser);
  }
}

async function shot(page, label) {
  fs.mkdirSync(paths.debug, { recursive: true });
  const file = path.join(paths.debug, `t2-${label}-${new Date().toISOString().replace(/[:.]/g, '-')}.png`);
  await page.screenshot({ path: file });
  return file;
}

// T9 — CDP attach
test('T9 attaches over CDP and finds an existing context', async () => {
  const { browser, context } = await attach();
  try {
    assert.ok(browser.contexts().length > 0, 'no contexts — did something launch a fresh browser?');
    assert.ok(context, 'contexts()[0] must be the real logged-in context');
  } finally {
    await detach(browser);
  }
  console.log(`T9 ok — attached to ${CDP_ENDPOINT}`);
});

// T10 — session alive
test('T10 /feed/ loads without redirecting to login', async () => {
  await withPage(async (page) => {
    const resp = await page.goto('https://www.linkedin.com/feed/', { waitUntil: 'domcontentloaded', timeout: 45000 });
    const status = resp?.status();
    const url = page.url();
    const reason = detectChallenge({ status, url, html: await page.content() });
    assert.equal(reason, null, `challenge/breaker condition on /feed/: ${reason}`);
    assert.ok(!/\/login|\/uas\/login|\/authwall/.test(url), `redirected to ${url} — session is not logged in`);
    await page.locator(SELECTORS.feedMarker).first().waitFor({ state: 'visible', timeout: 15000 });
  });
});

// T11 — REGRESSION GUARD against ever switching to launch mode
test('T11 navigator.webdriver is false/undefined (attached, not launched)', async () => {
  await withPage(async (page) => {
    await page.goto('https://www.linkedin.com/feed/', { waitUntil: 'domcontentloaded', timeout: 45000 });
    const wd = await page.evaluate(() => navigator.webdriver);
    assert.ok(
      wd === false || wd === undefined,
      `navigator.webdriver === ${wd}. Something switched to launch mode — this MUST stay an attach-only setup.`,
    );
  });
});

// T12 — selector probe, ONE profile, no click
//
// This test used to take `.first()` and assert only that SOME Connect control
// existed. It passed happily while pointing at a stranger: run against
// /in/williamhgates/ it reported "Invite Adeeb Zaatar to connect" — a member
// from the recommendations rail. It was validating the very bug the sender had.
// It now probes what the sender actually does: a NAME-SCOPED lookup.
test('T12 the Connect control is resolved BY NAME, never by position', { skip: !TEST_PROFILE ? 'set LINKEDIN_TEST_PROFILE' : false }, async () => {
  await withPage(async (page) => {
    const resp = await page.goto(TEST_PROFILE, { waitUntil: 'domcontentloaded', timeout: 45000 });
    const reason = detectChallenge({ status: resp?.status(), url: page.url(), html: await page.content() });
    assert.equal(reason, null, `breaker condition on the probe profile: ${reason}`);

    const connects = page.locator(SELECTORS.connectButton);
    const n = await connects.count();
    const file = await shot(page, 'selector-probe');
    console.log(`T12 screenshot: ${file}`);
    console.log(`T12 — ${n} "Invite … to connect" control(s) in <main> on this page`);

    // The selector must still match SOMETHING, or LinkedIn has reskinned.
    const more = await page.locator(SELECTORS.moreButton).count();
    assert.ok(n > 0 || more > 0,
      'neither an Invite control nor a More menu matched. Fix lib/selectors.js — do not guess at runtime.');

    // THE REAL CONTRACT: a name that is not on this page must resolve to
    // nothing. If this ever returns a control, the sender would invite a
    // stranger and mark the intended lead as sent.
    const impostor = await findControlFor(connects, 'Zzz Notarealperson');
    assert.equal(impostor, null,
      'a name absent from the page resolved to a control — the sender would invite the wrong person');

    // If you name the page's owner, report whether they are connectable — but
    // do NOT assert it. Whether a given profile offers Connect is LinkedIn's
    // call: Follow-only profiles, existing connections and already-pending
    // invites all legitimately have none. /in/williamhgates/ is the worked
    // example — 2 Invite controls on the page, both belonging to rail members,
    // none to him. "No control names him, so we click nothing" IS the correct
    // outcome, and the whole point of the fix.
    if (TEST_NAME) {
      const hit = await findControlFor(connects, TEST_NAME);
      console.log(hit
        ? `T12 ok — resolved by name: ${hit.label}`
        : `T12 ok — no control names "${TEST_NAME}" (Follow-only / already connected / pending); `
          + `the sender correctly skips rather than clicking one of the ${n} rail buttons`);
    } else {
      console.log('T12 ok — name scoping verified negatively; set LINKEDIN_TEST_NAME for the positive case');
    }
  });
});

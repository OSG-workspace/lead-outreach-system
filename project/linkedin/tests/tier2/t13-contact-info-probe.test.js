/*
 * TIER 2 — read-only against LinkedIn, with your real attached Chrome.
 * Nothing here clicks anything but the Contact Info trigger itself, and it
 * closes the panel again. T13 opens ONE profile and stops.
 *
 * WHY THIS TEST EXISTS
 * scripts/lookup_contact_email.js was written from documented LinkedIn markup,
 * not from a live probe (no browser access in that session). Every other
 * selector in lib/selectors.js earned its place by being proven against a real
 * page here first — this is the same discipline, applied to the three new
 * ones (profileContactInfoTrigger, contactInfoDialog, contactInfoEmailLink)
 * before eu-hotels' "linkedin-email-lookup" channel is trusted at volume.
 *
 * Prereqs:
 *   Chrome running with --remote-debugging-port=9222, logged in to LinkedIn.
 *   LINKEDIN_TEST_PROFILE=https://www.linkedin.com/in/<someone>/
 *
 * Run: npm run test:tier2
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { attach, detach } from '../../lib/browser.js';
import { SELECTORS } from '../../lib/selectors.js';
import { detectChallenge } from '../../lib/breakers.js';
import { paths } from '../../lib/paths.js';

const TEST_PROFILE = process.env.LINKEDIN_TEST_PROFILE;

async function withPage(fn) {
  const { browser, context } = await attach();
  const page = await context.newPage();
  try {
    return await fn(page, { browser, context });
  } finally {
    await page.close().catch(() => {});
    await detach(browser);
  }
}

async function shot(page, label) {
  fs.mkdirSync(paths.debug, { recursive: true });
  const file = path.join(paths.debug, `t13-${label}-${new Date().toISOString().replace(/[:.]/g, '-')}.png`);
  await page.screenshot({ path: file });
  return file;
}

test('T13 the Contact Info trigger and overlay resolve on a real profile',
  { skip: !TEST_PROFILE ? 'set LINKEDIN_TEST_PROFILE' : false }, async () => {
    await withPage(async (page) => {
      const resp = await page.goto(TEST_PROFILE, { waitUntil: 'domcontentloaded', timeout: 45000 });
      const reason = detectChallenge({ status: resp?.status(), url: page.url(), html: await page.content() });
      assert.equal(reason, null, `breaker condition on the probe profile: ${reason}`);

      const trigger = page.locator(SELECTORS.profileContactInfoTrigger).first();
      const hasTrigger = await trigger.isVisible({ timeout: 8000 }).catch(() => false);
      const before = await shot(page, 'before-click');
      console.log(`T13 screenshot (before): ${before}`);

      // The trigger must exist on SOME real profile, or the selector is dead
      // and lookup_contact_email.js reports "no email" for every lead forever
      // without ever actually looking. Fix lib/selectors.js — do not guess.
      assert.ok(hasTrigger,
        'profileContactInfoTrigger matched nothing on a real profile. LinkedIn has ' +
        'reskinned this control — update lib/selectors.js before trusting linkedin-email-lookup.');

      await trigger.click({ timeout: 12000 }).catch(() => {});
      const dialog = page.locator(SELECTORS.contactInfoDialog).first();
      const opened = await dialog.isVisible({ timeout: 8000 }).catch(() => false);
      assert.ok(opened,
        'contactInfoDialog never became visible after clicking the trigger. ' +
        'Either the click selector or the dialog selector is wrong.');

      await new Promise((r) => setTimeout(r, 800));
      const emailLink = page.locator(SELECTORS.contactInfoEmailLink).first();
      const hasEmail = await emailLink.isVisible({ timeout: 4000 }).catch(() => false);
      const after = await shot(page, 'contact-info-open');
      console.log(`T13 screenshot (contact-info open): ${after}`);

      // NOT asserted either way — most profiles legitimately show no email.
      // The point of this run is confirming the SELECTOR reads the panel
      // correctly when a person's own profile is in front of it, which the
      // screenshot lets a human verify directly.
      if (hasEmail) {
        const href = await emailLink.getAttribute('href').catch(() => null);
        console.log(`T13 ok — contactInfoEmailLink matched: ${href}`);
      } else {
        console.log('T13 ok — dialog opened correctly, no email listed on this profile ' +
          '(expected for most people; check the screenshot to confirm the panel really is empty, ' +
          'not that the selector missed a differently-marked-up email row)');
      }
    });
  });

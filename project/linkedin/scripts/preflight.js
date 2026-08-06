#!/usr/bin/env node
/*
 * FAIL IN FIVE SECONDS, NOT IN EIGHT MINUTES.
 *
 * Everything a LinkedIn run needs from the browser is knowable before the run
 * starts: the debug port, a logged-in session, the right account, and no active
 * cooldown. Without this check those are discovered at Stage 8.6 — after
 * sourcing, fetching, extracting and qualifying have already burned several
 * minutes and a chunk of the OSM city ledger for a run that was never going to
 * be able to send. run_fire.py calls this FIRST on any LinkedIn run.
 *
 * Read-only. Opens one tab, reads where /in/me/ redirects to, closes it.
 *
 * exit 0  ready
 * exit 2  not ready — stdout says exactly what to do about it
 */
import { paths } from '../lib/paths.js';
import { loadState } from '../lib/state.js';
import { cooldownActive } from '../lib/breakers.js';
import { attach, detach, finish } from '../lib/browser.js';
import { whoami } from '../lib/identity.js';
import { loadAccount } from '../lib/account.js';

function fail(msg, fix) {
  console.log(`LINKEDIN PREFLIGHT FAILED: ${msg}`);
  if (fix) console.log(`  fix: ${fix}`);
  finish(2);
}

async function main() {
  // 1. Cooldown — a tripped breaker means the account is resting. Starting a
  //    run that cannot send would waste the sourcing ground it consumes.
  const state = loadState(paths.state);
  if (cooldownActive(state, Date.now())) {
    const until = new Date(state.cooldownUntil).toISOString();
    fail(`breaker cooldown active until ${until}`,
         'wait it out — this is the anti-restriction guard, do not override it');
  }

  // 2. The debug port.
  let browser;
  try {
    ({ browser } = await attach());
  } catch (e) {
    fail('cannot attach to Chrome on the debug port',
         'bash linkedin/scripts/start_chrome.sh');
  }

  // 3. Logged in, and as the right person.
  const context = browser.contexts()[0];
  const page = await context.newPage();
  let slug = null;
  try {
    slug = await whoami(page);
  } catch (e) {
    await page.close().catch(() => {});
    await detach(browser);
    fail(`could not read the session: ${e.message}`, 'is that window actually on LinkedIn?');
  }
  await page.close().catch(() => {});
  await detach(browser);

  if (!slug) {
    fail('attached to Chrome, but that window is NOT logged in to LinkedIn',
         'log in in the Chrome window that start_chrome.sh opened');
  }
  const expected = loadAccount();
  if (expected && expected !== slug) {
    fail(`wrong account attached: expected "${expected}", found "${slug}"`,
         'check which Chrome profile holds the debug port');
  }

  console.log(`LinkedIn preflight OK — sending as ${slug}`
    + (expected ? '' : ' (no expected account recorded; `node scripts/whoami.js --save` to lock it in)'));
  // MUST exit explicitly: the CDP socket keeps the loop alive, and run_fire.py
  // waits on this process. Returning here hung every LinkedIn fire at Stage 0.
  finish(0);
}

main();

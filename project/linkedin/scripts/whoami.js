#!/usr/bin/env node
/*
 * Which LinkedIn account is on the debug port? Read-only, sends nothing.
 *
 * Run this after starting Chrome and before the first live send. It is the one
 * command that answers "is this really my account" without touching anyone.
 *
 * USAGE  node scripts/whoami.js
 *        node scripts/whoami.js --save    # record it as the expected account,
 *                                         # after which the senders REFUSE to
 *                                         # run against any other profile
 */
import fs from 'node:fs';
import path from 'node:path';
import { paths } from '../lib/paths.js';
import { attach, detach, finish } from '../lib/browser.js';
import { whoami } from '../lib/identity.js';
import { loadAccount, ACCOUNT_FILE } from '../lib/account.js';

async function main() {
  const save = process.argv.includes('--save');
  let browser;
  try {
    ({ browser } = await attach());
  } catch (e) {
    console.error(`[whoami] ${e.message}`);
    console.error('[whoami] start it with:  bash scripts/start_chrome.sh');
    finish(2);
  }
  const context = browser.contexts()[0];
  const page = await context.newPage();
  let slug = null;
  try {
    slug = await whoami(page);
  } finally {
    await page.close().catch(() => {});
    await detach(browser);
  }

  if (!slug) {
    console.error('[whoami] attached, but NOT logged in to LinkedIn in that window.');
    console.error('[whoami] log in there, then run this again.');
    finish(1);
  }

  console.log(`[whoami] attached account: ${slug}`);
  console.log(`[whoami] profile: https://www.linkedin.com/in/${slug}/`);

  const expected = loadAccount();
  if (save) {
    fs.mkdirSync(path.dirname(ACCOUNT_FILE), { recursive: true });
    fs.writeFileSync(ACCOUNT_FILE, JSON.stringify({ expectProfile: slug }, null, 2) + '\n');
    console.log(`[whoami] saved as the expected account -> ${ACCOUNT_FILE}`);
    console.log('[whoami] the senders will now refuse to run against any other profile.');
  } else if (expected && expected !== slug) {
    console.error(`[whoami] WARNING: expected "${expected}" per config/account.json — senders will refuse.`);
    finish(1);
  } else if (expected) {
    console.log(`[whoami] matches config/account.json (${expected}) — senders will run.`);
  } else {
    console.log('[whoami] no expected account recorded yet. Run with --save to lock it in.');
  }
  // The CDP socket keeps node alive; without this whoami never returns.
  finish(0);
}

main();

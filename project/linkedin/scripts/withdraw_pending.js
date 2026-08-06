#!/usr/bin/env node
/*
 * RETIRE STALE INVITES. The guardrail `withdrawPendingAfterDays` was supposed
 * to provide and never did.
 *
 * Until 2026-08-03 that setting appeared in exactly one place: the list of keys
 * limits.js requires to be present. Nothing read it, nothing withdrew anything,
 * and pending invites accumulated forever. Two consequences, both bad:
 *
 *   1. ACCOUNT RISK. A large pile of unanswered invitations is one of the
 *      clearest mass-inviting signals LinkedIn has. It is precisely the shape
 *      of an account that adds strangers indiscriminately.
 *   2. A SELF-INFLICTED HALT. trailingAcceptance counts every still-`sent`
 *      invite as a non-acceptance, so the pile drags the trailing rate toward
 *      zero. Below the 25% floor, sender.js stops issuing new invites — a
 *      permanent stop caused by bookkeeping, reported as a performance problem.
 *
 * Withdrawing an invite is also the polite outcome for the recipient: it
 * disappears from their pending list rather than sitting there indefinitely.
 *
 * DISCIPLINE: identical to the senders. Attach-only, one tab at a time,
 * human-paced, no guessed selectors, challenge trips the same 48h cooldown.
 * A withdrawal is not a send, so it does NOT consume the invite ramp — but it
 * is still a write action, so it is paced and capped.
 *
 * USAGE  node scripts/withdraw_pending.js            # report only
 *        node scripts/withdraw_pending.js --commit   # actually withdraw
 */
import { paths } from '../lib/paths.js';
import { loadLimits } from '../lib/limits.js';
import { loadState, saveState, markStatus, stalePending, normalizeProfileUrl } from '../lib/state.js';
import { attach, detach, finish } from '../lib/browser.js';
import { HardStop, detectChallenge, cooldownActive, startCooldown } from '../lib/breakers.js';
import { sleep, dwellMs } from '../lib/pacing.js';
import { alert } from '../lib/alerts.js';
import { SELECTORS } from '../lib/selectors.js';
import { findControlFor } from '../lib/target.js';

const T = 10000;
const MAX_PER_RUN = 15;      // withdrawals are cheap, but never a burst

async function gotoChecked(page, url) {
  const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const hit = detectChallenge({ status: resp?.status(), url: page.url(), html: await page.content().catch(() => '') });
  if (hit) throw new HardStop(`challenge detected (${hit})`, 'challenge');
}

async function main() {
  const commit = process.argv.includes('--commit');
  const limits = loadLimits();
  const state = loadState(paths.state);

  if (cooldownActive(state, Date.now())) {
    alert(paths.alerts, 'HALT', 'cooldown active — refusing to withdraw');
    finish(10);
    return;
  }

  const stale = stalePending(state, limits.withdrawPendingAfterDays);
  console.log(`[withdraw] ${stale.length} invite(s) pending longer than `
    + `${limits.withdrawPendingAfterDays} days`);
  if (!stale.length) { finish(0); return; }

  const batch = stale.slice(0, MAX_PER_RUN);
  if (!commit) {
    for (const l of batch) console.log(`  would withdraw: ${l.name ?? l.profileUrl} (sent ${l.sentAt})`);
    console.log('[withdraw] REPORT ONLY — pass --commit to actually withdraw.');
    finish(0);
    return;
  }

  const { browser, context } = await attach();
  let done = 0; let code = 0;
  try {
    for (const lead of batch) {
      const page = await context.newPage();
      try {
        await gotoChecked(page, lead.profileUrl);
        await sleep(dwellMs(limits));

        // The pending control names the person, exactly like the Connect one,
        // so the same rule applies: withdraw only if it is demonstrably THEIRS.
        // The rail is full of other members' buttons.
        const pending = await findControlFor(page.locator(SELECTORS.pendingButton), lead.name);
        if (!pending) {
          // No longer pending: they accepted, it expired, or it was withdrawn
          // elsewhere. The acceptance sweep is what decides which — not us.
          console.log(`[withdraw] no pending control for ${lead.name ?? lead.profileUrl} — leaving for the sweep`);
          await page.close().catch(() => {});
          continue;
        }
        await pending.locator.click({ timeout: T });
        const confirm = page.locator(SELECTORS.withdrawConfirmButton).first();
        await confirm.waitFor({ state: 'visible', timeout: T });
        await confirm.click();

        markStatus(state, normalizeProfileUrl(lead.profileUrl) ?? lead.profileUrl, 'withdrawn',
                   { withdrawnAt: new Date().toISOString() });
        saveState(paths.state, state);
        done++;
        console.log(`[withdraw] withdrew ${lead.name ?? lead.profileUrl}`);
      } catch (e) {
        if (e instanceof HardStop && e.kind === 'challenge') {
          startCooldown(state); saveState(paths.state, state);
          alert(paths.alerts, 'HALT', `withdraw: ${e.message} — 48h cooldown`);
          code = 10; await page.close().catch(() => {}); break;
        }
        // Never retry, never guess — same contract as the senders.
        alert(paths.alerts, 'WARN', `withdraw failed for ${lead.profileUrl}: ${e.message}`);
      }
      await page.close().catch(() => {});
      await sleep(5000 + Math.random() * 9000);
    }
  } finally {
    saveState(paths.state, state);
    await detach(browser);
  }
  console.log(`[withdraw] done — ${done} withdrawn.`);
  finish(code);
}

main().catch((e) => { alert(paths.alerts, 'FATAL', `withdraw: ${e.message}`); finish(1); });

#!/usr/bin/env node
/*
 * ACTION LAYER. Dumb, deterministic, zero LLM calls.
 *
 * Reads queue.json, enforces every limit itself, sends, updates state.
 * Defaults to --dry-run; live sending requires an explicit --live.
 * Never auto-retries a send: a failed invite may have landed, so it goes to
 * review-required.json for a human.
 */
import fs from 'node:fs';
import path from 'node:path';
import { paths } from '../lib/paths.js';
import {
  loadLimits, capForDay, isSendDay, isInSendWindow, dateKeyInTz,
  minutesOfDay, minutesOfDayInTz,
} from '../lib/limits.js';
import {
  loadState, saveState, reconcileAttempting, markAttempting, markSent, markStatus,
  appendReview, invitesToday, invitesInRollingWeek, campaignDay, trailingAcceptance,
  outstandingInvites,
} from '../lib/state.js';
import {
  detectChallenge, acceptanceBreach, weeklyBreach, cooldownActive, startCooldown, HardStop,
} from '../lib/breakers.js';
import { nextGapMinutes, dwellMs, sleep } from '../lib/pacing.js';
import { SELECTORS } from '../lib/selectors.js';
import { attach, detach, finish } from '../lib/browser.js';
import { assertAccount } from '../lib/identity.js';
import { loadAccount } from '../lib/account.js';
import { alert } from '../lib/alerts.js';
import { acquireLock, LockBusy, LOCK_BUSY_EXIT } from '../lib/lock.js';
import { findControlFor } from '../lib/target.js';

const SELECTOR_TIMEOUT_MS = 12000;

async function screenshot(page, label) {
  fs.mkdirSync(paths.debug, { recursive: true });
  const file = path.join(paths.debug, `${new Date().toISOString().replace(/[:.]/g, '-')}-${label}.png`);
  await page.screenshot({ path: file, fullPage: false }).catch(() => {});
  return file;
}

/** Navigate and run the challenge breaker over the result. */
async function gotoChecked(page, url) {
  const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  const status = resp ? resp.status() : null;
  const html = await page.content().catch(() => '');
  const reason = detectChallenge({ status, url: page.url(), html });
  if (reason) throw new HardStop(reason, 'challenge');
  return { status, url: page.url() };
}

/**
 * Locate the Connect control FOR THIS PERSON. Not found => stop, never guess.
 *
 * `expectedName` is neither optional nor cosmetic. A profile page also carries
 * an `Invite <Name> to connect` button for every member in the recommendations
 * rail — SEVEN of them on the profile probed 2026-08-03, not one belonging to
 * the page's owner. Taking `.first()`, as this used to, invites a stranger and
 * then records the intended lead as sent: two people burned per click. So the
 * control must NAME the person we came for; if none does we return null and the
 * caller stops.
 */
async function locateConnect(page, expectedName) {
  if (!expectedName) return null;        // cannot verify => must not click

  const direct = await findControlFor(page.locator(SELECTORS.connectButton), expectedName);
  if (direct) {
    await direct.locator.waitFor({ state: 'visible', timeout: SELECTOR_TIMEOUT_MS });
    return { locator: direct.locator, via: 'top-card', label: direct.label };
  }

  // The More menu is only the right one if IT names the person too.
  const more = await findControlFor(page.locator(SELECTORS.moreButton), expectedName);
  if (more) {
    await more.locator.click({ timeout: SELECTOR_TIMEOUT_MS });
    const item = await findControlFor(page.locator(SELECTORS.moreConnectItem), expectedName);
    if (!item) return null;
    await item.locator.waitFor({ state: 'visible', timeout: SELECTOR_TIMEOUT_MS });
    return { locator: item.locator, via: 'more-menu', label: item.label };
  }
  return null;
}

/**
 * Send the invite. `note` is normally null.
 *
 * A free LinkedIn account gets roughly FIVE personalised invitation notes per
 * month; past that the "Add a note" control is not rendered at all. Waiting for
 * it unconditionally therefore trips the missing-selector breaker on about the
 * sixth invite and every one after, which would look like a broken selector
 * rather than an account limit. So a note is opt-in: with none, the invite goes
 * out bare and the personalisation lands in the DM after acceptance instead.
 */
async function sendInvite(page, note) {
  if (note) {
    await page.locator(SELECTORS.addNoteButton).first().waitFor({ state: 'visible', timeout: SELECTOR_TIMEOUT_MS });
    await page.locator(SELECTORS.addNoteButton).first().click();
    const box = page.locator(SELECTORS.noteTextarea).first();
    await box.waitFor({ state: 'visible', timeout: SELECTOR_TIMEOUT_MS });
    await box.fill(note);
  }
  const send = page.locator(SELECTORS.sendInviteButton).first();
  await send.waitFor({ state: 'visible', timeout: SELECTOR_TIMEOUT_MS });
  await send.click();
  // Confirmation: the top-card control flips to Pending. No flip = unknown outcome.
  await page.locator(SELECTORS.pendingButton).first().waitFor({ state: 'visible', timeout: SELECTOR_TIMEOUT_MS });
}

async function main() {
  const argv = process.argv.slice(2);
  const live = argv.includes('--live');
  const dryRun = !live; // default is dry-run, always
  const ignoreSchedule = argv.includes('--now');

  // 1. Config guard first — refuse to start on a bad config.
  let limits;
  try {
    limits = loadLimits();
  } catch (e) {
    alert(paths.alerts, 'FATAL', `config guard: ${e.message}`);
    process.exit(2);
  }

  let release;
  try {
    release = acquireLock(paths.lock);
  } catch (e) {
    if (!(e instanceof LockBusy)) throw e;
    console.log(`[sender] ${e.message} — skipping this invocation.`);
    process.exit(LOCK_BUSY_EXIT);
  }
  const now = new Date();
  const state = loadState(paths.state);

  // 2. Crash recovery BEFORE anything else. Stale "attempting" => unknown, never retried.
  const stale = reconcileAttempting(state, paths.review);
  if (stale.length) {
    alert(paths.alerts, 'WARN', `${stale.length} unresolved attempt(s) moved to review-required.json`, {
      profiles: stale.map((l) => l.profileUrl),
    });
    saveState(paths.state, state);
  }

  // 3. Breakers that stop us before we ever attach.
  const cooling = cooldownActive(state, now.getTime());
  if (cooling) {
    alert(paths.alerts, 'HALT', `cooldown active until ${cooling}`);
    release(); process.exit(3);
  }
  if (!isSendDay(limits, now)) {
    console.log(`[sender] ${dateKeyInTz(now, limits.timezone)} is not a send day — nothing to do.`);
    release(); return;
  }
  const week = invitesInRollingWeek(state, limits.timezone, now);
  const weekly = weeklyBreach(limits, week);
  if (weekly) {
    alert(paths.alerts, 'HALT', weekly);
    release(); process.exit(4);
  }
  // THE PENDING-PILE CEILING. A big stack of unanswered invitations is one of
  // the clearest mass-inviting signals LinkedIn has, and it also drags
  // trailingAcceptance toward zero until the floor breaker halts us for a
  // bookkeeping reason. Nothing capped it before. Stop adding to the pile and
  // let scripts/withdraw_pending.js drain it instead.
  const outstanding = outstandingInvites(state);
  if (outstanding >= limits.maxOutstandingInvites) {
    alert(paths.alerts, 'HALT',
      `${outstanding} invites still pending (max ${limits.maxOutstandingInvites}) — `
      + 'not adding to the pile. Run scripts/withdraw_pending.js --commit to retire stale ones.');
    release(); process.exit(8);
  }

  const acceptance = acceptanceBreach(limits, trailingAcceptance(state, limits.acceptanceFloorSampleSize));
  if (acceptance) {
    state.invitesPaused = true;
    saveState(paths.state, state);
    alert(paths.alerts, 'HALT', `${acceptance} — new invites stopped (follow-up DMs may continue)`);
    release(); process.exit(5);
  }

  // 4. Queue + independent cap enforcement.
  if (!fs.existsSync(paths.queue)) {
    alert(paths.alerts, 'WARN', 'no queue.json — generator did not run?');
    release(); process.exit(6);
  }
  const queue = JSON.parse(fs.readFileSync(paths.queue, 'utf8'));
  const today = dateKeyInTz(now, limits.timezone);
  if (queue.date !== today) {
    alert(paths.alerts, 'WARN', `queue.json is for ${queue.date}, today is ${today} — refusing to send`);
    release(); process.exit(7);
  }

  const cap = capForDay(limits, campaignDay(state, limits.timezone, now));
  const alreadySent = invitesToday(state, limits.timezone, now);
  const weekRoom = Math.max(0, limits.weeklyMax - week);
  const room = Math.max(0, Math.min(cap - alreadySent, limits.absoluteDailyMax - alreadySent, weekRoom));

  const pending = queue.entries
    .filter((e) => (state.leads[e.profileUrl]?.status ?? null) === null || state.leads[e.profileUrl]?.status === 'queued')
    .slice(0, room);

  console.log(
    `[sender] ${dryRun ? 'DRY RUN' : 'LIVE'} — day ${queue.campaignDay}, cap ${cap}, sent today ${alreadySent}, ` +
    `week ${week}/${limits.weeklyMax}, sending ${pending.length}`,
  );
  if (pending.length === 0) { release(); return; }

  const { browser, context } = await attach();

  // WHO ARE WE SENDING AS? Checked, never assumed — these invites go out under
  // a real person's name, and attaching to whatever is on the debug port is not
  // proof it is theirs. Also catches the commonest real failure: a Chrome that
  // is attached fine but silently logged out, which would otherwise burn a
  // whole day's ramp clicking on login walls.
  {
    const probe = await context.newPage();
    try {
      const acct = await assertAccount(probe, loadAccount());
      console.log(`[sender] sending as ${acct}`);
    } catch (e) {
      alert(paths.alerts, 'HALT', `identity check failed: ${e.message}`);
      await probe.close().catch(() => {});
      await detach(browser);
      process.exit(2);
    }
    await probe.close().catch(() => {});
  }

  let consecutiveFailures = 0;
  let sentThisRun = 0;
  let lastActionAt = null;
  let exitCode = 0;

  try {
    for (let i = 0; i < pending.length; i++) {
      const entry = pending[i];

      // Pacing: honour the planned slot, but re-randomise the gap at action time.
      if (!ignoreSchedule) {
        const planned = Date.parse(entry.scheduledAt);
        let earliest = planned;
        if (lastActionAt) {
          const gapMin = nextGapMinutes(limits, i, Math.random);
          earliest = Math.max(planned, lastActionAt + gapMin * 60000);
        }
        const waitMs = earliest - Date.now();
        if (waitMs > 0) {
          console.log(`[sender] waiting ${(waitMs / 60000).toFixed(1)}min for ${entry.profileUrl}`);
          await sleep(waitMs);
        }
      }

      const at = new Date();
      if (!isInSendWindow(limits, at)) {
        alert(paths.alerts, 'WARN',
          `send window closed (${minutesOfDayInTz(at, limits.timezone)} vs ${minutesOfDay(limits.sendWindow.end)}) — stopping for the day`);
        break;
      }

      // One tab at a time, serialised. Never parallel.
      const page = await context.newPage();
      try {
        await gotoChecked(page, entry.profileUrl);
        await sleep(dwellMs(limits)); // dwell on the profile like a human reading it

        const connect = await locateConnect(page, entry.name);
        if (!connect) {
          // Could be a reskin, could be that this person simply offers no
          // Connect (Follow-only, or already pending). Either way we do NOT
          // click whatever else is on the page — the rail is full of other
          // members' Invite buttons. Skip this lead and keep the day going.
          const shot = await screenshot(page, 'selector-miss');
          markStatus(state, entry.profileUrl, 'queued',
                     { reason: `no Connect control naming "${entry.name}" on this page` });
          saveState(paths.state, state);
          alert(paths.alerts, 'WARN',
                `no Connect control naming "${entry.name}" on ${entry.profileUrl} — skipped, `
                + `not clicking another member's button (screenshot ${shot})`);
          await page.close().catch(() => {});
          continue;
        }

        if (dryRun) {
          const shot = await screenshot(page, 'dry-run');
          console.log(
            `[dry-run] WOULD click "${await connect.locator.getAttribute('aria-label').catch(() => '?')}" ` +
            `(${connect.via}) on ${entry.profileUrl}, then ` +
            (entry.note ? `Add a note -> "${entry.note}" -> ` : 'no note (free-account cap) -> ') +
            `Send invitation. Follow-up DM on acceptance: ` +
            `"${(entry.message ?? '').slice(0, 60)}…". Screenshot: ${shot}`,
          );
          consecutiveFailures = 0;
        } else {
          // Write "attempting" WITH a timestamp BEFORE the click, and fsync it.
          // `message` is the DM to send IF this invite is accepted, stored on
          // the lead now because acceptance happens days or weeks later, long
          // after this run's queue file is gone. This is the only place it is
          // persisted, so it must be written before the click, with everything
          // else the retry-safety story depends on.
          markAttempting(state, entry.profileUrl, {
            leadId: entry.leadId, name: entry.name, note: entry.note,
            message: entry.message ?? null, company: entry.company ?? null,
            companyDomain: entry.companyDomain ?? null,
            templated: entry.templated === true,
          });
          saveState(paths.state, state);

          await connect.locator.click({ timeout: SELECTOR_TIMEOUT_MS });
          await sendInvite(page, entry.note);

          markSent(state, entry.profileUrl, limits.timezone, new Date());
          saveState(paths.state, state);
          sentThisRun++;
          consecutiveFailures = 0;
          console.log(`[sender] sent -> ${entry.profileUrl}`);
        }
        lastActionAt = Date.now();
      } catch (e) {
        lastActionAt = Date.now();
        if (e instanceof HardStop && e.kind === 'challenge') {
          const until = startCooldown(state, Date.now());
          markStatus(state, entry.profileUrl, dryRun ? 'queued' : 'unknown', { reason: e.message });
          if (!dryRun) appendReview(paths.review, [state.leads[entry.profileUrl]]);
          saveState(paths.state, state);
          alert(paths.alerts, 'HALT', `circuit breaker: ${e.message} — cooldown until ${until}`);
          await page.close().catch(() => {});
          exitCode = 10;
          break;
        }
        if (e instanceof HardStop && e.kind === 'selector') {
          markStatus(state, entry.profileUrl, dryRun ? 'queued' : 'unknown', { reason: e.message });
          if (!dryRun) appendReview(paths.review, [state.leads[entry.profileUrl]]);
          saveState(paths.state, state);
          alert(paths.alerts, 'HALT', `circuit breaker: ${e.message} — not guessing an alternative selector`);
          await page.close().catch(() => {});
          exitCode = 11;
          break;
        }
        // Any other failure: NEVER auto-retry. The invite may already have landed.
        consecutiveFailures++;
        markStatus(state, entry.profileUrl, dryRun ? 'queued' : 'unknown', { reason: e.message });
        if (!dryRun) appendReview(paths.review, [state.leads[entry.profileUrl]]);
        saveState(paths.state, state);
        alert(paths.alerts, 'WARN', `send failed for ${entry.profileUrl}: ${e.message} — manual review, no retry`);
        await screenshot(page, 'failure').catch(() => {});
        if (consecutiveFailures >= 2) {
          alert(paths.alerts, 'HALT', 'two consecutive failures — stopping for the day');
          await page.close().catch(() => {});
          exitCode = 12;
          break;
        }
      }
      await page.close().catch(() => {}); // page.close(), never browser.close()
    }
  } finally {
    await detach(browser);
    release();
  }

  console.log(`[sender] done — ${dryRun ? 0 : sentThisRun} invite(s) sent this run.`);
  // Always exit, including on success: after attach() the CDP socket keeps
  // the process alive, so returning here left the sender running forever
  // holding paths.lock — and the next morning's run refused as 'lock busy'.
  finish(exitCode);
}

main().catch((e) => {
  alert(paths.alerts, 'FATAL', e.message);
  process.exit(1);
});

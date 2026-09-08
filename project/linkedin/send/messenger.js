#!/usr/bin/env node
/*
 * ACTION LAYER — DIRECT MESSAGES. Sibling of sender.js, same discipline:
 * zero LLM calls, every limit re-derived here from config/limits.json, one tab
 * at a time, no guessed selectors, no auto-retry, breakers halt the run.
 *
 * WHY A SEPARATE PROCESS FROM sender.js
 * An invite and a DM are different acts with different ceilings and different
 * consequences. Invites are the scarce, account-risky resource (ramp 8->18/day,
 * weeklyMax 90) because pending piles trigger restrictions. DMs to people who
 * already accepted are cheap and safe, and have their own budget
 * (followUpDmsPerDay, 25). Mixing them in one loop would force one cap onto
 * both. The acceptance-floor breaker in sender.js stops NEW INVITES while
 * explicitly allowing follow-up DMs to continue — that only works if the two
 * are separately runnable.
 *
 * WHO CAN ACTUALLY BE MESSAGED
 * LinkedIn allows a free DM only to a 1st-degree connection or an Open Profile
 * member. Everyone else needs an InMail credit. So this process does NOT create
 * reach; it spends reach that already exists. The top-card Message button is
 * the ground truth: if it is absent, the person is skipped, never "upgraded"
 * into a connect. If the compose that opens is an InMail (subject field
 * present), it is ABORTED unless --allow-inmail is passed, so a run can never
 * silently burn paid credits.
 *
 * INPUT   state/dm-queue.json   { date, entries:[{profileUrl, message, ...}] }
 * USAGE   node send/messenger.js              # DRY RUN (default)
 *         node send/messenger.js --live
 *         node send/messenger.js --live --allow-inmail
 */
import fs from 'node:fs';
import path from 'node:path';
import { paths } from '../lib/paths.js';
import { loadLimits, dateKeyInTz, isSendDay, isInSendWindow, minutesOfDayInTz, minutesOfDay, dmCapForDay } from '../lib/limits.js';
import { loadState, saveState, markAttempting, markSent, markStatus, appendReview, reconcileAttempting,
         campaignDay, dmsToday, dmsInRollingWeek } from '../lib/state.js';
import { attach, detach } from '../lib/browser.js';
import { assertAccount } from '../lib/identity.js';
import { loadAccount } from '../lib/account.js';
import { HardStop, detectChallenge, cooldownActive, startCooldown } from '../lib/breakers.js';
import { nextGapMinutes, dwellMs, sleep } from '../lib/pacing.js';
import { alert } from '../lib/alerts.js';
import { SELECTORS } from '../lib/selectors.js';
import { acquireLock, LockBusy, LOCK_BUSY_EXIT } from '../lib/lock.js';
import { findControlFor } from '../lib/target.js';

const SELECTOR_TIMEOUT_MS = 10000;
const DM_QUEUE = paths.dmQueue;

async function screenshot(page, label) {
  const dir = path.join(paths.root, 'debug');
  fs.mkdirSync(dir, { recursive: true });
  const f = path.join(dir, `${label}-${Date.now()}.png`);
  try { await page.screenshot({ path: f, fullPage: false }); } catch {}
  return f;
}

async function gotoChecked(page, url) {
  const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const html = (await page.content().catch(() => '')).slice(0, 4000);
  const hit = detectChallenge({ status: resp?.status(), url: page.url(), html });
  if (hit) throw new HardStop(`challenge detected (${hit})`, 'challenge');
}

/**
 * Ground truth for "can I message THIS person at all".
 *
 * Same trap as the invite button: the recommendations rail carries a Message
 * button for every other member on the page, so `.first()` could open a chat
 * with a stranger and send them a DM written for someone else. The control
 * must name the person we came for.
 */
async function locateMessage(page, expectedName) {
  if (!expectedName) return null;      // cannot verify => must not click
  const hit = await findControlFor(page.locator(SELECTORS.messageButton), expectedName);
  if (!hit) return null;               // not 1st-degree, not open profile, or not this page
  try {
    await hit.locator.waitFor({ state: 'visible', timeout: SELECTOR_TIMEOUT_MS });
    return hit.locator;
  } catch {
    return null;
  }
}

async function sendMessage(page, msgBtn, text, { allowInmail, dryRun }) {
  // Click the VERIFIED button we were handed, never a fresh .first() lookup.
  await msgBtn.click();
  await page.locator(SELECTORS.messageOverlay).first()
    .waitFor({ state: 'visible', timeout: SELECTOR_TIMEOUT_MS });

  // Never spend an InMail credit by accident.
  const isInmail = await page.locator(SELECTORS.inmailSubject).first()
    .isVisible({ timeout: 1500 }).catch(() => false);
  if (isInmail && !allowInmail) {
    throw new HardStop('compose opened as an InMail (paid credit) — skipping; ' +
                       'pass --allow-inmail to permit', 'inmail');
  }

  const box = page.locator(SELECTORS.messageBox).first();
  await box.waitFor({ state: 'visible', timeout: SELECTOR_TIMEOUT_MS });
  if (dryRun) return { isInmail, typed: false };

  await box.click();
  // Type in chunks with human-ish latency rather than pasting a block.
  for (const line of String(text).split('\n')) {
    await box.type(line, { delay: 18 + Math.floor(Math.random() * 32) });
    await page.keyboard.press('Shift+Enter');
    await sleep(120 + Math.random() * 260);
  }
  const send = page.locator(SELECTORS.messageSendButton).first();
  await send.waitFor({ state: 'visible', timeout: SELECTOR_TIMEOUT_MS });
  await send.click();
  return { isInmail, typed: true };
}

async function main() {
  const argv = process.argv.slice(2);
  const dryRun = !argv.includes('--live');
  const allowInmail = argv.includes('--allow-inmail');
  const ignoreSchedule = argv.includes('--now');

  const limits = loadLimits();
  const state = loadState(paths.state);
  const now = new Date();

  if (cooldownActive(state, now.getTime())) {
    alert(paths.alerts, 'HALT', 'cooldown active — refusing to message');
    process.exit(10);
  }
  if (!isSendDay(limits, now) && !argv.includes('--force')) {
    console.log('[messenger] not a send day — nothing to do.');
    return;
  }

  // The old lock here had no liveness check and — worse — was never released on
  // the identity-failure exit below, so one logged-out morning left a lockfile
  // that made every subsequent run exit "lock held" forever. lib/lock.js checks
  // the holder's PID and registers its release on process exit, so no exit path
  // can leak it.
  let release;
  try {
    release = acquireLock(paths.messengerLock);
  } catch (e) {
    if (!(e instanceof LockBusy)) throw e;
    console.log(`[messenger] ${e.message} — skipping this invocation.`);
    process.exit(LOCK_BUSY_EXIT);
  }
  reconcileAttempting(state, paths.review);

  if (!fs.existsSync(DM_QUEUE)) {
    alert(paths.alerts, 'WARN', 'no dm-queue.json');
    release(); process.exit(6);
  }
  const queue = JSON.parse(fs.readFileSync(DM_QUEUE, 'utf8'));
  const today = dateKeyInTz(now, limits.timezone);
  if (queue.date !== today) {
    alert(paths.alerts, 'WARN', `dm-queue.json is for ${queue.date}, today is ${today} — refusing`);
    release(); process.exit(7);
  }

  // DMs have their OWN budget, independent of the invite ramp — but a budget
  // with a ramp and a weekly ceiling, not a flat number. Re-derived HERE, never
  // trusted from the queue: the generator may propose more than is allowed and
  // the worst it can do is have this truncate the list.
  const day = campaignDay(state, limits.timezone, now);
  const dayCap = dmCapForDay(limits, day);
  const sentToday = dmsToday(state, limits.timezone, now);
  const week = dmsInRollingWeek(state, limits.timezone, now);
  const weekRoom = Math.max(0, limits.dmWeeklyMax - week);
  if (weekRoom === 0) {
    alert(paths.alerts, 'HALT',
      `dmWeeklyMax reached (${week}/${limits.dmWeeklyMax}) — no DMs until the rolling 7-day window clears`);
    release(); process.exit(4);
  }
  const room = Math.max(0, Math.min(dayCap - sentToday, weekRoom));
  const pending = queue.entries
    // A journey entry carries its own step gate (generate-dm.js checks the step
    // history), so a follow-up is allowed past the blanket "already DMed" filter.
    .filter((e) => e.journeyStep || state.leads[e.profileUrl]?.dmStatus !== 'sent')
    .slice(0, room);

  console.log(`[messenger] ${dryRun ? 'DRY RUN' : 'LIVE'} — day ${day}, dm cap ${dayCap}, ` +
              `sent today ${sentToday}, week ${week}/${limits.dmWeeklyMax}, sending ${pending.length}`);
  if (!pending.length) { release(); return; }

  const { browser, context } = await attach();

  // Same identity gate as sender.js — a DM is even more clearly "from" a person
  // than an invite is, so it must not go out from an unverified session.
  {
    const probe = await context.newPage();
    try {
      const acct = await assertAccount(probe, loadAccount());
      console.log(`[messenger] messaging as ${acct}`);
    } catch (e) {
      alert(paths.alerts, 'HALT', `identity check failed: ${e.message}`);
      await probe.close().catch(() => {});
      await detach(browser);
      process.exit(2);
    }
    await probe.close().catch(() => {});
  }

  let consecutiveFailures = 0, lastActionAt = null, exitCode = 0;

  try {
    for (let i = 0; i < pending.length; i++) {
      const entry = pending[i];
      if (!ignoreSchedule && lastActionAt) {
        const gapMin = nextGapMinutes(limits, i, Math.random);
        const waitMs = lastActionAt + gapMin * 60000 - Date.now();
        if (waitMs > 0) {
          console.log(`[messenger] waiting ${(waitMs / 60000).toFixed(1)}min`);
          await sleep(waitMs);
        }
      }
      if (!isInSendWindow(limits, new Date())) {
        alert(paths.alerts, 'WARN', 'send window closed — stopping for the day');
        break;
      }

      const page = await context.newPage();
      try {
        await gotoChecked(page, entry.profileUrl);
        await sleep(dwellMs(limits));

        const msgBtn = await locateMessage(page, entry.name);
        if (!msgBtn) {
          // Not messageable. This is a normal, expected outcome — NOT a failure,
          // and explicitly NOT a reason to fall back to sending a connect.
          markStatus(state, entry.profileUrl, 'queued',
                     { dmStatus: 'not-messageable', reason: 'no Message button (not 1st-degree / not open profile)' });
          saveState(paths.state, state);
          console.log(`[messenger] skip (not messageable) -> ${entry.profileUrl}`);
          await page.close();
          continue;
        }

        if (dryRun) {
          console.log(`[messenger] WOULD message ${entry.profileUrl}: ${JSON.stringify(entry.message).slice(0, 120)}…`);
          await sendMessage(page, msgBtn, entry.message, { allowInmail, dryRun: true });
          await screenshot(page, 'dm-dryrun');
        } else {
          markAttempting(state, entry.profileUrl, { leadId: entry.leadId, name: entry.name, dm: true });
          saveState(paths.state, state);
          const { isInmail } = await sendMessage(page, msgBtn, entry.message, { allowInmail, dryRun: false });
          const rec = state.leads[entry.profileUrl] || {};
          rec.dmStatus = 'sent';
          rec.dmSentDay = today;
          rec.dmSentAt = new Date().toISOString();
          rec.dmChannel = isInmail ? 'linkedin-inmail' : 'linkedin-dm';
          rec.status = rec.status === 'attempting' ? (rec.priorStatus || 'sent') : rec.status;
          state.leads[entry.profileUrl] = rec;
          saveState(paths.state, state);
          console.log(`[messenger] sent -> ${entry.profileUrl}`);
        }
        consecutiveFailures = 0;
        lastActionAt = Date.now();
      } catch (e) {
        if (e instanceof HardStop && e.kind === 'inmail') {
          // Not a failure of the run — a deliberate refusal to spend a credit.
          markStatus(state, entry.profileUrl, 'queued', { dmStatus: 'inmail-skipped' });
          saveState(paths.state, state);
          console.log(`[messenger] skip (would cost an InMail) -> ${entry.profileUrl}`);
          await page.close().catch(() => {});
          continue;
        }
        const shot = await screenshot(page, 'dm-fail');
        markStatus(state, entry.profileUrl, dryRun ? 'queued' : 'unknown',
                   { dmStatus: dryRun ? 'queued' : 'unknown', reason: e.message });
        if (!dryRun) appendReview(paths.review, [state.leads[entry.profileUrl]]);
        saveState(paths.state, state);
        if (e instanceof HardStop && e.kind === 'challenge') {
          startCooldown(state); saveState(paths.state, state);
          alert(paths.alerts, 'HALT', `circuit breaker: ${e.message} — 48h cooldown`);
          exitCode = 10; await page.close().catch(() => {}); break;
        }
        if (e instanceof HardStop) {
          alert(paths.alerts, 'HALT', `circuit breaker: ${e.message} (screenshot ${shot}) — not guessing a selector`);
          exitCode = 11; await page.close().catch(() => {}); break;
        }
        alert(paths.alerts, 'WARN', `dm failed for ${entry.profileUrl}: ${e.message} — manual review, no retry`);
        if (++consecutiveFailures >= 2) {
          alert(paths.alerts, 'HALT', 'two consecutive failures — stopping for the day');
          exitCode = 12; await page.close().catch(() => {}); break;
        }
      }
      await page.close().catch(() => {});
    }
  } finally {
    await detach(browser);
    saveState(paths.state, state);
    release();
  }
  process.exit(exitCode);
}

if (import.meta.url === `file://${process.argv[1]}`) main();

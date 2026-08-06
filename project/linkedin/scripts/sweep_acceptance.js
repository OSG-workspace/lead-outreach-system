#!/usr/bin/env node
/*
 * THE MISSING HALF OF THE TWO-PHASE CHAIN: who accepted?
 *
 * An invite and its follow-up DM are separated by days or weeks. sender.js
 * sends the invite and stores the composed DM on the lead; this sweep is what
 * notices the acceptance, flips the lead to `accepted`, and thereby releases
 * that DM to generate-dm.js. Without it the chain sends invites into a void:
 * every invited person stays `sent` forever, generate-dm.js rejects them all
 * as "not accepted", and no message is ever delivered.
 *
 * It also repairs the acceptance-floor breaker. lib/state.js#trailingAcceptance
 * counts leads with status 'accepted', and nothing in the module ever set that
 * status — so once 100 invites had gone out the trailing rate read 0%, below
 * the 25% floor, and the breaker would have stopped new invites permanently
 * while reporting a performance problem that was really a bookkeeping gap.
 *
 * METHOD, AND WHY IT IS CHEAP
 * The sent-invitations manager lists everything still PENDING in one place.
 * Anything we marked `sent` that is no longer listed there has changed state,
 * so only those few profiles need visiting to tell an acceptance from an
 * expiry. A run therefore costs ~2 list pages plus one visit per CHANGED
 * invite, not one visit per invite ever sent.
 *
 * DISCIPLINE: same as sender.js. Read-only apart from state.json, attaches to
 * the logged-in Chrome, never launches, one tab at a time, human-paced,
 * challenge trips the 48h cooldown, and a missing selector STOPS the sweep
 * rather than guessing.
 *
 * USAGE  node scripts/sweep_acceptance.js            # report only
 *        node scripts/sweep_acceptance.js --commit   # write state.json
 */
import fs from 'node:fs';
import path from 'node:path';
import { paths } from '../lib/paths.js';
import { loadLimits } from '../lib/limits.js';
import { loadState, saveState, markStatus, normalizeProfileUrl } from '../lib/state.js';
import { attach, detach } from '../lib/browser.js';
import { HardStop, detectChallenge, cooldownActive, startCooldown } from '../lib/breakers.js';
import { sleep, dwellMs } from '../lib/pacing.js';
import { alert } from '../lib/alerts.js';
import { SELECTORS } from '../lib/selectors.js';
import { parseDegree } from './walk_companies.js';

const T = 12000;
const SENT_INVITES_URL = 'https://www.linkedin.com/mynetwork/invitation-manager/sent/';

async function screenshot(page, label) {
  fs.mkdirSync(paths.debug, { recursive: true });
  const f = path.join(paths.debug, `${label}-${Date.now()}.png`);
  try { await page.screenshot({ path: f }); } catch {}
  return f;
}

async function gotoChecked(page, url) {
  const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const html = (await page.content().catch(() => '')).slice(0, 4000);
  const hit = detectChallenge({ status: resp?.status(), url: page.url(), html });
  if (hit) throw new HardStop(`challenge detected (${hit})`, 'challenge');
}

/** Every profile URL with an invite still awaiting a response. */
export async function readPendingInvites(page, maxPages = 5) {
  await gotoChecked(page, SENT_INVITES_URL);
  await sleep(dwellMs(loadLimits()));
  const pending = new Set();
  for (let p = 0; p < maxPages; p++) {
    const cards = page.locator(SELECTORS.sentInvitationCard);
    const n = await cards.count().catch(() => 0);
    if (n === 0 && p === 0) {
      // Zero cards has two opposite meanings and the difference decides whether
      // DMs go out, so it is resolved against LinkedIn's OWN empty state rather
      // than guessed. Every invite eventually leaves this list, so "genuinely
      // empty" is not an edge case — it is the steady state of a healthy
      // campaign, and treating it as a broken selector (as this did) meant the
      // sweep began failing permanently at exactly the moment the invites were
      // all being answered, releasing no DM ever again.
      const empty = await page.locator(SELECTORS.sentInvitationsEmpty).first()
        .isVisible({ timeout: 4000 }).catch(() => false);
      if (empty) {
        console.log('[sweep] LinkedIn reports no pending sent invitations.');
        return pending;                 // empty set: every sent invite has changed state
      }
      throw new HardStop(
        'sent-invitations list matched nothing AND no empty-state marker was found — '
        + 'cannot tell "no pending invites" from a changed selector, and the '
        + 'difference decides whether DMs go out',
        'selector');
    }
    for (let i = 0; i < n; i++) {
      const href = await cards.nth(i).locator(SELECTORS.sentInvitationLink).first()
        .getAttribute('href').catch(() => null);
      const url = normalizeProfileUrl(href);
      if (url) pending.add(url);
    }
    const next = page.locator(SELECTORS.sentInvitationsNext).first();
    if (!(await next.isEnabled({ timeout: 2000 }).catch(() => false))) break;
    await next.click().catch(() => {});
    await sleep(2500 + Math.random() * 3500);
  }
  return pending;
}

/** Ground truth for one person: 1st degree means the invite was accepted. */
async function confirmAccepted(page, profileUrl) {
  await gotoChecked(page, profileUrl);
  await sleep(dwellMs(loadLimits()));
  const badge = await page.locator(SELECTORS.profileDegree).first()
    .innerText({ timeout: 4000 }).catch(() => '');
  const top = await page.locator('main').first().innerText({ timeout: T }).catch(() => '');
  return (parseDegree(badge) ?? parseDegree(top.slice(0, 400))) === 1;
}

async function main() {
  const commit = process.argv.includes('--commit');
  const limits = loadLimits();
  const state = loadState(paths.state);
  if (cooldownActive(state, Date.now())) {
    alert(paths.alerts, 'HALT', 'cooldown active — refusing to sweep');
    process.exit(10);
  }

  const invited = Object.values(state.leads || {}).filter((l) => l.status === 'sent');
  if (invited.length === 0) {
    console.log('[sweep] no pending invites in state — nothing to do.');
    return;
  }

  const { browser, context } = await attach();
  const page = await context.newPage();
  let accepted = 0; let expired = 0; let exitCode = 0;
  try {
    const pending = await readPendingInvites(page);
    console.log(`[sweep] ${invited.length} invites marked sent, ${pending.size} still pending on LinkedIn`);

    for (const lead of invited) {
      const url = normalizeProfileUrl(lead.profileUrl);
      if (pending.has(url)) continue;               // still waiting, no visit needed
      await sleep(3000 + Math.random() * 7000);
      const isAccepted = await confirmAccepted(page, lead.profileUrl);
      if (isAccepted) {
        accepted++;
        console.log(`[sweep] ACCEPTED ${lead.name ?? url}` + (lead.message ? ' (DM ready)' : ' (no DM stored)'));
        if (commit) markStatus(state, url, 'accepted', { acceptedAt: new Date().toISOString() });
      } else {
        expired++;
        console.log(`[sweep] no longer pending, not connected: ${lead.name ?? url}`);
        if (commit) markStatus(state, url, 'expired', { expiredAt: new Date().toISOString() });
      }
    }
  } catch (e) {
    const shot = await screenshot(page, 'sweep-fail');
    if (e instanceof HardStop && e.kind === 'challenge') {
      startCooldown(state); saveState(paths.state, state);
      alert(paths.alerts, 'HALT', `sweep: ${e.message} — 48h cooldown`);
      exitCode = 10;
    } else {
      alert(paths.alerts, 'HALT', `sweep stopped: ${e.message} (screenshot ${shot})`);
      exitCode = e instanceof HardStop ? 11 : 1;
    }
  } finally {
    await page.close().catch(() => {});
    await detach(browser);
    if (commit && exitCode !== 10) saveState(paths.state, state);
  }

  const withDm = Object.values(state.leads || {})
    .filter((l) => l.status === 'accepted' && l.message && l.dmStatus !== 'sent').length;
  console.log(`[sweep] +${accepted} accepted, +${expired} expired/withdrawn`
    + (commit ? '' : ' (REPORT ONLY — pass --commit to write state.json)'));
  console.log(`[sweep] ${withDm} accepted people have a composed DM waiting.`);
  console.log('[sweep] next: node queue/generate-dm.js --from-state && node send/messenger.js --live');
  process.exit(exitCode);
}

if (import.meta.url === `file://${process.argv[1]}`) main();

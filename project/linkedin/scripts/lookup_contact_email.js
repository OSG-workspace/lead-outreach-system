#!/usr/bin/env node
/*
 * Stage 5.6 (EMAIL-CHANNEL RESCUE): LinkedIn profile -> a personal email, when
 * one is publicly listed in the profile owner's Contact Info.
 *
 * WHY THIS EXISTS (2026-08-20)
 * name-finder routinely finds the right decision-maker but no personal email
 * — on 2026-08-20-eu-hotels that was 80 of 99 qualified leads, all for the
 * same reason: the business site publishes only a generic mailbox
 * (info@, reservations@, hotel@...). The name and role were already paid for
 * and are not in question; only the address is missing. Some professionals
 * list a personal email in LinkedIn's Contact Info panel even when their
 * employer's site never does. This script is the one extra check for that,
 * so a lead dies only when BOTH the site and LinkedIn have nothing — not
 * when the site alone has nothing.
 *
 * This does NOT send an invite or a DM and is NOT subject to the
 * invite/DM ramp in config/limits.json — it only reads a public profile
 * panel, the same class of action as a company-page walk. It still shares
 * the channel's account-safety discipline: attach-only, one tab at a time,
 * human pacing, and the same challenge/cooldown breaker as everything else
 * that touches this Chrome session.
 *
 * DISCIPLINE (same as walk_companies.js / sender.js)
 * Read-only. Attaches to the logged-in Chrome, never launches. Selectors live
 * in lib/selectors.js and a missing selector STOPS that profile (recorded as
 * "not found") rather than guessing at alternative markup. A real challenge
 * page trips the shared 48h cooldown and halts the whole run.
 *
 * UNVERIFIED SELECTORS — see lib/selectors.js and
 * tests/tier2/t13-contact-info-probe.test.js. This script has not been run
 * against a live profile. It fails closed: if Chrome is not attached, or the
 * contact-info selectors do not match anything, it writes empty/partial
 * results and exits 0 rather than blocking the email-only run it was called
 * from.
 *
 * INPUT   --in <file.json>   [{lead_id, url}]  (LinkedIn profile URLs)
 * OUTPUT  --out <file.json>  [{lead_id, url, email_found, email, note}]
 * USAGE   node scripts/lookup_contact_email.js --in in.json --out out.json
 *         [--max-profiles 40]
 */
import fs from 'node:fs';
import path from 'node:path';
import { paths } from '../lib/paths.js';
import { loadLimits } from '../lib/limits.js';
import { loadState, saveState } from '../lib/state.js';
import { attach, detach } from '../lib/browser.js';
import { HardStop, detectChallenge, cooldownActive, startCooldown } from '../lib/breakers.js';
import { sleep, dwellMs } from '../lib/pacing.js';
import { alert } from '../lib/alerts.js';
import { SELECTORS } from '../lib/selectors.js';

const T = 12000;

function arg(name, dflt = null) {
  const i = process.argv.indexOf(name);
  return i === -1 ? dflt : process.argv[i + 1];
}

async function screenshot(page, label) {
  const dir = path.join(paths.root, 'debug');
  fs.mkdirSync(dir, { recursive: true });
  const f = path.join(dir, `${label}-${Date.now()}.png`);
  try { await page.screenshot({ path: f }); } catch {}
  return f;
}

async function gotoChecked(page, url) {
  const resp = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  const html = (await page.content().catch(() => '')).slice(0, 4000);
  const hit = detectChallenge({ status: resp?.status(), url: page.url(), html });
  if (hit) throw new HardStop(`challenge detected (${hit})`, 'challenge');
  return resp;
}

/** mailto:foo@bar.com?subject=... -> foo@bar.com */
function emailFromMailto(href) {
  const raw = String(href || '').replace(/^mailto:/i, '');
  return decodeURIComponent(raw.split('?')[0]).trim().toLowerCase();
}

async function lookupOne(page, url) {
  await gotoChecked(page, url);
  await sleep(dwellMs(loadLimits()));

  const trigger = page.locator(SELECTORS.profileContactInfoTrigger).first();
  const hasTrigger = await trigger.isVisible({ timeout: 4000 }).catch(() => false);
  if (!hasTrigger) {
    return { email_found: false, note: 'no contact-info trigger on profile (selector may be stale, or LinkedIn simply has none to show)' };
  }
  await trigger.click({ timeout: T }).catch(() => {});
  const dialog = page.locator(SELECTORS.contactInfoDialog).first();
  const opened = await dialog.isVisible({ timeout: 6000 }).catch(() => false);
  if (!opened) {
    return { email_found: false, note: 'contact-info trigger clicked but no dialog opened (selector may be stale)' };
  }
  await sleep(600 + Math.random() * 900);   // let the overlay finish rendering
  const emailLink = page.locator(SELECTORS.contactInfoEmailLink).first();
  const hasEmail = await emailLink.isVisible({ timeout: 4000 }).catch(() => false);
  let result;
  if (hasEmail) {
    const href = await emailLink.getAttribute('href').catch(() => null);
    const email = href ? emailFromMailto(href) : '';
    result = email
      ? { email_found: true, email, note: 'mailto: link in profile Contact Info' }
      : { email_found: false, note: 'mailto: link present but unparseable' };
  } else {
    result = { email_found: false, note: 'contact-info panel opened, no email listed' };
  }
  // Close before leaving — an overlay left open on the next goto() is a
  // harmless no-op for us, but there is no reason to leave state dirty.
  const closeBtn = page.locator(SELECTORS.contactInfoCloseButton).first();
  await closeBtn.click({ timeout: 2000 }).catch(async () => { await page.keyboard.press('Escape').catch(() => {}); });
  return result;
}

async function main() {
  const inFile = arg('--in');
  const outFile = arg('--out');
  // Conservative default: this is unbudgeted, unverified traffic on a real
  // account (see file header). Small by design — the caller sizes the input
  // list, this is just a hard backstop against a mis-sized caller.
  const maxProfiles = Number(arg('--max-profiles', '40'));
  if (!inFile || !outFile) {
    console.error('usage: lookup_contact_email.js --in <leads.json> --out <results.json> [--max-profiles 40]');
    process.exit(2);
  }

  const leads = JSON.parse(fs.readFileSync(inFile, 'utf8')).slice(0, maxProfiles);
  console.log(`[lookup] ${leads.length} LinkedIn profile(s) to check for a Contact Info email`);
  if (leads.length === 0) {
    fs.writeFileSync(outFile, '[]');
    return;
  }

  const state = loadState(paths.state);
  if (cooldownActive(state, Date.now())) {
    console.log('[lookup] cooldown active on this channel — skipping entirely, not just invites.');
    fs.writeFileSync(outFile, '[]');
    return;
  }

  let browser;
  let context;
  try {
    ({ browser, context } = await attach());
  } catch (e) {
    // Fails CLOSED but non-fatal: an email-only run must not break because
    // the LinkedIn Chrome session isn't open. That is an expected state for
    // most fires — this rescue is opportunistic, not required.
    console.log(`[lookup] could not attach to LinkedIn Chrome (${e.message}); skipping this rescue, ` +
                'the affected leads stay dropped.');
    fs.writeFileSync(outFile, '[]');
    return;
  }

  const results = [];
  let exitCode = 0;
  try {
    for (const lead of leads) {
      const page = await context.newPage();
      try {
        const r = await lookupOne(page, lead.url);
        results.push({ lead_id: lead.lead_id, url: lead.url, ...r });
        console.log(`[lookup] ${lead.lead_id}: ${r.email_found ? `found ${r.email}` : `no email (${r.note})`}`);
      } catch (e) {
        if (e instanceof HardStop && e.kind === 'challenge') {
          const shot = await screenshot(page, 'lookup-challenge');
          startCooldown(state); saveState(paths.state, state);
          alert(paths.alerts, 'HALT', `lookup_contact_email: ${e.message} — 48h cooldown (screenshot ${shot})`);
          await page.close().catch(() => {});
          exitCode = 10;
          break;
        }
        alert(paths.alerts, 'WARN', `lookup_contact_email failed for ${lead.lead_id}: ${e.message}`);
        results.push({ lead_id: lead.lead_id, url: lead.url, email_found: false, note: `error: ${e.message}` });
      }
      await page.close().catch(() => {});
      await sleep(4000 + Math.random() * 8000);   // human pacing between profiles
    }
  } finally {
    await detach(browser);
    fs.writeFileSync(outFile, JSON.stringify(results, null, 2));
    const found = results.filter((r) => r.email_found).length;
    console.log(`[lookup] wrote ${results.length} result(s), ${found} email(s) recovered -> ${outFile}`);
  }
  process.exit(exitCode);
}

main().catch((e) => { console.error('[lookup] fatal:', e); process.exit(1); });

#!/usr/bin/env node
/*
 * Stage 5.5-LI (ACTION LAYER): company -> people.
 *
 * WHY COMPANY-FIRST AND NOT SALES NAV SEARCH
 * The /fire chain already enumerates companies precisely and exhaustively from
 * OSM, with city ledgers, dedup and geography solved. Walking from a known
 * company to its people reuses all of that and touches LinkedIn far less than
 * bulk-scraping search results would — fewer pages, narrower queries, and every
 * request anchored to a company we independently verified exists. Person-first
 * Sales Navigator sourcing is the better tool where OSM is empty (Beirut
 * contractors, Riyadh clinics), but it needs a Sales Nav seat and much heavier
 * search traffic, so it is deliberately not the default.
 *
 * DISCIPLINE (same as sender.js)
 * Read-only. Attaches to the logged-in Chrome, never launches. One tab at a
 * time. Human-paced. Selectors live in lib/selectors.js and a missing selector
 * STOPS the walk with a screenshot rather than guessing an alternative.
 * Challenge/CAPTCHA trips the same breaker and the same 48h cooldown.
 *
 * GEO ANCHOR: it records the COMPANY's location from the company page, which is
 * what qualify_people.py filters on. The person's own profile location is
 * captured but deliberately NOT used for filtering — a "Beirut" profile living
 * in Dubai is the most common false positive in Lebanese sourcing.
 *
 * INPUT   --companies <file.json>  [{name, domain, country, city, vertical}]
 * OUTPUT  --out <people-raw.json>  records for qualify_people.py
 * USAGE   node scripts/walk_companies.js --companies c.json --out people-raw.json
 *         [--max-per-company 5] [--limit 40] [--dry-run]
 */
import fs from 'node:fs';
import path from 'node:path';
import { paths } from '../lib/paths.js';
import { loadLimits } from '../lib/limits.js';
import { loadState, saveState, normalizeProfileUrl } from '../lib/state.js';
import { attach, detach } from '../lib/browser.js';
import { HardStop, detectChallenge, cooldownActive, startCooldown } from '../lib/breakers.js';
import { sleep, dwellMs } from '../lib/pacing.js';
import { alert } from '../lib/alerts.js';
import { SELECTORS } from '../lib/selectors.js';

const T = 12000;

/**
 * LinkedIn writes ages as "2h", "3d", "1w", "2mo", "1yr". Convert to days.
 * `mo` MUST be tested before `m` or every month reads as a minute and a
 * year-dormant account looks alive.
 */
export function ageToDays(text) {
  const m = /(\d+)\s*(mo|yr|y|w|d|h|m|s)\b/i.exec(String(text ?? ''));
  if (!m) return null;
  const n = Number(m[1]);
  switch (m[2].toLowerCase()) {
    case 'yr': case 'y':  return n * 365;
    case 'mo':            return n * 30;
    case 'w':             return n * 7;
    case 'd':             return n;
    case 'h': case 'm': case 's': return 0;   // today
    default:              return null;
  }
}

/**
 * "11-50 employees" -> 11. The LOWER bound is deliberate: it is the
 * conservative read for the `small_firm_headcount` gate, which RESTRICTS how
 * many people we may invite at one company. Taking the low end means a
 * "1-10 employees" firm counts as small and gets the one-invite-per-window
 * treatment, so an ambiguous range errs toward fewer invites, never more.
 */
export function parseHeadcount(text) {
  const m = /([\d,]+)\s*(?:-|–|to)?\s*([\d,]*)\s*employees/i.exec(String(text ?? ''));
  if (!m) return null;
  return Number(m[1].replace(/,/g, '')) || null;
}

/** "3rd" / "2nd degree" -> 3 / 2. Drives reachability, and invites vs DMs. */
export function parseDegree(text) {
  const m = /\b([123])(?:st|nd|rd)\b/i.exec(String(text ?? ''));
  return m ? Number(m[1]) : null;
}

export function parseMutuals(text) {
  const m = /([\d,]+)\s+(?:other\s+)?mutual/i.exec(String(text ?? ''));
  return m ? Number(m[1].replace(/,/g, '')) : null;
}

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

/** Resolve a company name to its LinkedIn company URL via LinkedIn's own search. */
async function findCompany(page, company) {
  const q = encodeURIComponent(company.name);
  await gotoChecked(page, `https://www.linkedin.com/search/results/companies/?keywords=${q}`);
  await sleep(dwellMs(loadLimits()));
  const link = page.locator('a[href*="/company/"]').first();
  try {
    await link.waitFor({ state: 'visible', timeout: T });
  } catch {
    return null;                       // no company page — normal, skip quietly
  }
  const href = await link.getAttribute('href');
  const m = /\/company\/([^/?#]+)/.exec(href || '');
  return m ? `https://www.linkedin.com/company/${m[1]}/` : null;
}

/** Scrape the company's People tab for profiles whose title matches. */
async function walkPeople(page, companyUrl, titleRe, maxPer) {
  await gotoChecked(page, `${companyUrl}people/`);
  await sleep(dwellMs(loadLimits()));

  // Company location + size, for the geo anchor and the small-firm gate. Read
  // from the company page itself.
  let companyLocation = null;
  let companyHeadcount = null;
  try {
    const info = await page.locator(SELECTORS.companySizeLine).allInnerTexts();
    companyLocation = (info[0] ?? '').trim() || null;
    for (const line of info) {
      const hc = parseHeadcount(line);
      if (hc !== null) { companyHeadcount = hc; break; }
    }
  } catch { /* absent on some pages; qualify_people falls back to fixture geo */ }

  // Lazy list: scroll a bounded number of times, human-paced.
  for (let i = 0; i < 4; i++) {
    await page.mouse.wheel(0, 1400);
    await sleep(900 + Math.random() * 1400);
  }

  const cards = page.locator(SELECTORS.companyPeopleCard);
  let n = 0;
  try { n = await cards.count(); } catch { n = 0; }
  // ZERO CARDS IS NORMAL, NOT A BROKEN SELECTOR — for ONE company.
  // Plenty of real Gulf businesses have a LinkedIn company page whose People tab
  // lists nobody (no employee has tagged the company, or the tab is gated for a
  // non-connection). This used to throw the selector breaker, which broke out of
  // the company loop on the FIRST such page and exited 11 — and run_fire treats
  // that as fatal, so a single empty People tab killed the entire fire before
  // anything was queued. The reskin signal is not "one page had none", it is
  // "NO page had any", and only main() can see that. So report the miss and let
  // it decide.
  if (n === 0) return { people: [], listed: false };

  const out = [];
  for (let i = 0; i < n && out.length < maxPer; i++) {
    const c = cards.nth(i);
    const text = (await c.innerText().catch(() => '')) || '';
    const href = await c.locator(SELECTORS.companyProfileLink).first().getAttribute('href').catch(() => null);
    if (!href) continue;
    const [nameLine, ...rest] = text.split('\n').map((s) => s.trim()).filter(Boolean);
    const title = rest.find((r) => r.length > 2) || '';
    if (titleRe && !titleRe.test(title)) continue;
    // ONE canonical spelling, identical to tools/scripts/li_url.py and to the
    // form generate.js dedups on. Raw hrefs vary (trailing slash, `ae.` locale
    // subdomain, tracking query), and every downstream join is a string match:
    // a person spelled two ways loses their composed DM at the merge.
    const canonical = normalizeProfileUrl(href);
    if (!canonical) continue;
    out.push({
      full_name: nameLine || null,
      title,
      profile_url: canonical,
      profile_location: null,       // captured, never used for filtering
      company_location_raw: companyLocation,
      company_headcount_li: companyHeadcount,
    });
  }
  return { people: out, listed: true };
}

/**
 * Visit ONE person's profile and read the fields the gates test.
 *
 * WHY THIS EXISTS AT ALL
 * qualify_people.py gates on aliveness (last_activity_days) and reachability
 * (degree / open_profile). Neither is visible on the company People tab, so
 * without this step both are undefined for every person, `alive` and
 * `reachable` are both false, and the arm qualifies nobody — which is exactly
 * what it did. A gate whose input is never harvested is not a strict gate, it
 * is an unconditional reject.
 *
 * It is also where the personalization hooks come from. li-writer's messages
 * are rejected as near-duplicates if they are all generic, so "no hooks" does
 * not degrade the run gracefully — it fails it.
 *
 * Cost: two page loads per person. Everything is best-effort EXCEPT the top
 * card itself; a missing optional field is recorded as null, never guessed.
 */
async function harvestProfile(page, profileUrl, { withActivity = true } = {}) {
  const out = {
    degree: null, open_profile: false, mutual_connections: null,
    school: null, last_activity_days: null, recent_posts: [],
  };
  await gotoChecked(page, profileUrl);
  await sleep(dwellMs(loadLimits()));

  const top = await page.locator('main').first().innerText({ timeout: T }).catch(() => '');
  out.degree = parseDegree(await page.locator(SELECTORS.profileDegree).first()
    .innerText({ timeout: 3000 }).catch(() => '')) ?? parseDegree(top.slice(0, 400));
  out.mutual_connections = parseMutuals(top);

  // Open Profile: a Message button on a NON-1st-degree profile means anyone may
  // message them for free. On a 1st-degree connection the button is expected
  // and says nothing, so it must not be read as an open profile.
  const canMessage = await page.locator(SELECTORS.profileOpenBadge).first()
    .isVisible({ timeout: 3000 }).catch(() => false);
  out.open_profile = canMessage && out.degree !== 1;

  const edu = await page.locator(SELECTORS.profileEducation).allInnerTexts().catch(() => []);
  out.school = edu.map((s) => s.trim()).filter(Boolean)[0] ?? null;

  if (withActivity) {
    // Recent activity lives on its own page; its relative timestamps are the
    // only public source for "when was this person last alive here".
    await sleep(1200 + Math.random() * 2000);
    await gotoChecked(page, `${profileUrl.replace(/\/$/, '')}/recent-activity/all/`);
    await sleep(dwellMs(loadLimits()));
    const items = await page.locator(SELECTORS.profileActivityItem).all().catch(() => []);
    const ages = [];
    for (const it of items.slice(0, 3)) {
      const t = (await it.innerText().catch(() => '')) || '';
      const d = ageToDays(t);
      if (d !== null) ages.push(d);
      const firstLine = t.split('\n').map((s) => s.trim()).filter(Boolean)
        .find((s) => s.length > 25 && !/^(likes?|comment|repost)/i.test(s));
      if (firstLine) out.recent_posts.push(firstLine.slice(0, 180));
    }
    out.last_activity_days = ages.length ? Math.min(...ages) : null;
  }
  return out;
}

/**
 * SECOND ROUTE: harvest an explicit list of profiles found by web search.
 *
 * Companies with no LinkedIn company page never reach the People tab, so
 * li-finder resolves their owner's profile URL from ordinary search results
 * instead. Those people still need exactly the same gate fields as everyone
 * else, so they come through the same harvestProfile() — the only difference is
 * how we learned the URL. Nothing downstream can tell the two routes apart, and
 * nothing downstream should.
 */
async function walkFoundProfiles(context, found, { maxPeople, skipActivity }) {
  const people = [];
  for (const person of found) {
    if (people.length >= maxPeople) {
      console.log(`[walk] reached --max-people ${maxPeople}; stopping.`);
      break;
    }
    const page = await context.newPage();
    try {
      const harvested = await harvestProfile(page, person.profile_url, { withActivity: !skipActivity });
      people.push({ ...person, ...harvested });
      console.log(`[walk] ${person.full_name ?? person.profile_url} (${person.company}) ` +
                  `degree=${harvested.degree ?? '?'} active=${harvested.last_activity_days ?? '?'}d`);
    } catch (e) {
      if (e instanceof HardStop) { await page.close().catch(() => {}); throw e; }
      alert(paths.alerts, 'WARN', `profile harvest failed ${person.profile_url}: ${e.message}`);
      people.push({ ...person });      // recorded, will fail the gates visibly
    }
    await page.close().catch(() => {});
    await sleep(4000 + Math.random() * 8000);
  }
  return people;
}

async function main() {
  const companiesFile = arg('--companies');
  const profilesFile = arg('--profiles');
  const outFile = arg('--out');
  const maxPer = Number(arg('--max-per-company', '5'));
  const limit = Number(arg('--limit', '40'));
  // Global ceiling on PROFILE visits, which are the expensive, risky page loads
  // (two each). A free account sends 8-18 invites/day and 90/week, so
  // harvesting hundreds of people in one sitting buys nothing and spends
  // account trust. Default 60 ~= three weeks of invite supply.
  const maxPeople = Number(arg('--max-people', '60'));
  const skipActivity = process.argv.includes('--no-activity');
  const dryRun = process.argv.includes('--dry-run');
  if ((!companiesFile && !profilesFile) || !outFile) {
    console.error('usage: walk_companies.js --companies <file> --out <file> [--max-per-company N] [--limit N]');
    console.error('       walk_companies.js --profiles <people-found.json> --out <file>');
    process.exit(2);
  }

  const state = loadState(paths.state);
  if (cooldownActive(state, Date.now())) {
    alert(paths.alerts, 'HALT', 'cooldown active — refusing to walk');
    process.exit(10);
  }

  // --profiles: harvest an explicit list li-finder resolved by web search.
  if (profilesFile) {
    const found = JSON.parse(fs.readFileSync(profilesFile, 'utf8'));
    console.log(`[walk] ${found.length} search-found profiles to harvest` + (dryRun ? ' (DRY RUN)' : ''));
    if (dryRun) {
      found.slice(0, 5).forEach((p) => console.log(`   ${p.full_name} — ${p.profile_url}`));
      fs.writeFileSync(outFile, '[]');
      return;
    }
    const { browser, context } = await attach();
    let harvested = [];
    let code = 0;
    try {
      harvested = await walkFoundProfiles(context, found, { maxPeople, skipActivity });
    } catch (e) {
      if (e instanceof HardStop && e.kind === 'challenge') {
        startCooldown(state); saveState(paths.state, state);
        alert(paths.alerts, 'HALT', `walk(profiles): ${e.message} — 48h cooldown`);
        code = 10;
      } else {
        alert(paths.alerts, 'HALT', `walk(profiles) stopped: ${e.message}`);
        code = 11;
      }
    } finally {
      await detach(browser);
      fs.writeFileSync(outFile, JSON.stringify(harvested, null, 2));
      console.log(`[walk] wrote ${harvested.length} harvested profiles -> ${outFile}`);
    }
    process.exit(code);
  }

  const companies = JSON.parse(fs.readFileSync(companiesFile, 'utf8')).slice(0, limit);
  const titleRe = process.env.LI_TITLE_REGEX ? new RegExp(process.env.LI_TITLE_REGEX, 'i') : null;
  console.log(`[walk] ${companies.length} companies, <=${maxPer} people each` +
              (titleRe ? `, titles ~ ${titleRe}` : '') + (dryRun ? ' (DRY RUN)' : ''));

  if (dryRun) {
    console.log('[walk] dry run: not attaching to Chrome. Would visit:');
    companies.slice(0, 5).forEach((c) => console.log(`   ${c.name}`));
    fs.writeFileSync(outFile, '[]');
    return;
  }

  const { browser, context } = await attach();
  const people = [];
  let exitCode = 0;
  // Selector health, measured across the WHOLE walk rather than per company:
  // an empty People tab is ordinary, every People tab being empty is a reskin.
  let pagesOpened = 0;
  let pagesWithPeople = 0;
  try {
    for (const co of companies) {
      const page = await context.newPage();
      try {
        const url = await findCompany(page, co);
        if (!url) { console.log(`[walk] no company page: ${co.name}`); await page.close(); continue; }
        const { people: found, listed } = await walkPeople(page, url, titleRe, maxPer);
        pagesOpened++;
        if (listed) pagesWithPeople++;
        else console.log(`[walk] ${co.name}: People tab lists nobody — skipping (li-finder will try the web)`);
        for (const p of found) {
          if (people.length >= maxPeople) break;
          // Visit the profile for the gate fields. A person we cannot harvest
          // is still recorded (with nulls) so the drop is visible in
          // people-dropped.json rather than silently vanishing here.
          let harvested = {};
          try {
            harvested = await harvestProfile(page, p.profile_url, { withActivity: !skipActivity });
            await sleep(3000 + Math.random() * 6000);
          } catch (e) {
            if (e instanceof HardStop) throw e;          // challenge -> breaker
            alert(paths.alerts, 'WARN', `profile harvest failed ${p.profile_url}: ${e.message}`);
          }
          people.push({
            ...p,
            ...harvested,
            company: co.name,
            company_domain: co.domain ?? null,
            // Geo anchor comes from the FIXTURE's own sourcing (OSM gave us the
            // city/country), which is more reliable than parsing the LI header.
            company_country: co.country ?? null,
            company_city: co.city ?? null,
            // Prefer the headcount LinkedIn states over the (usually absent)
            // OSM one; it is what the small-firm coordination gate reads.
            company_headcount: p.company_headcount_li ?? co.headcount ?? null,
            vertical: co.vertical ?? null,
          });
        }
        console.log(`[walk] ${co.name}: ${found.length} people (total ${people.length}/${maxPeople})`);
        if (people.length >= maxPeople) {
          console.log(`[walk] reached --max-people ${maxPeople}; stopping the walk here.`);
          await page.close().catch(() => {});
          break;
        }
      } catch (e) {
        const shot = await screenshot(page, 'walk-fail');
        if (e instanceof HardStop && e.kind === 'challenge') {
          startCooldown(state); saveState(paths.state, state);
          alert(paths.alerts, 'HALT', `walk: ${e.message} — 48h cooldown`);
          exitCode = 10; await page.close().catch(() => {}); break;
        }
        alert(paths.alerts, 'WARN', `walk failed for ${co.name}: ${e.message} (screenshot ${shot})`);
        if (e instanceof HardStop) { exitCode = 11; await page.close().catch(() => {}); break; }
      }
      await page.close().catch(() => {});
      await sleep(4000 + Math.random() * 9000);   // human pacing between companies
    }
    // NOW the reskin check has enough evidence to be meaningful. One empty
    // People tab says nothing; a dozen company pages that all opened fine and
    // all listed nobody says the card selector no longer matches. Warn but do
    // NOT fail the run: whatever the walk did find is real, and Stage 8.6b
    // resolves the rest by web search, so the fire still has a path to people.
    if (pagesOpened >= 5 && pagesWithPeople === 0) {
      alert(paths.alerts, 'HALT',
        `people-list selector matched nothing on ALL ${pagesOpened} company pages — `
        + `likely a LinkedIn reskin of ${SELECTORS.companyPeopleCard}. `
        + 'Re-probe it (npm run test:tier2) and update lib/selectors.js. '
        + 'Not guessing an alternative; this walk fell back to the search route only.');
    }
  } finally {
    await detach(browser);
    fs.writeFileSync(outFile, JSON.stringify(people, null, 2));
    console.log(`[walk] wrote ${people.length} people -> ${outFile}`
      + (pagesOpened ? ` (${pagesWithPeople}/${pagesOpened} company pages listed anyone)` : ''));
  }
  process.exit(exitCode);
}

if (import.meta.url === `file://${process.argv[1]}`) main();

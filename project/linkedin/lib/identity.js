/*
 * WHICH ACCOUNT AM I ACTUALLY DRIVING?
 *
 * The senders attach to whatever Chrome is listening on the debug port. That is
 * normally the right window — but "normally" is not good enough for something
 * that sends messages under a real person's name. If a different profile were
 * ever attached (a second Chrome started on another user-data-dir, a session
 * swapped out, a colleague's laptop), invites would go out from the wrong
 * account, and nothing downstream would notice.
 *
 * So the identity is CHECKED, not assumed, before anything is sent.
 *
 * The check is deliberately selector-free. `linkedin.com/in/me/` redirects to
 * the logged-in member's own profile, so the final URL IS the answer. No DOM
 * parsing, nothing to break when LinkedIn reskins the top card.
 */

/** -> "david-geha" (the logged-in member's own profile slug), or null. */
export function slugFromProfileUrl(url) {
  const m = /linkedin\.com\/in\/([^/?#]+)/i.exec(String(url ?? ''));
  if (!m) return null;
  const slug = decodeURIComponent(m[1]).toLowerCase();
  // "me" is the ALIAS WE ASKED FOR, never an answer. Seeing it back means the
  // redirect has not happened yet — see whoami() below.
  return slug === 'me' ? null : slug;
}

const ME_ALIAS = /\/in\/me\/?$/i;
const LOGGED_OUT = /\/(login|authwall|checkpoint|signup|uas\/login)/i;

/**
 * Resolve the attached session's own profile slug, or null if it is not a
 * usable logged-in session.
 *
 * WHY THIS WAITS INSTEAD OF READING THE URL STRAIGHT AWAY
 * `/in/me/` is an alias the server resolves — to the member's real profile when
 * logged in, to `/login` when not. Neither has happened at `domcontentloaded`,
 * so the previous version read the URL while it was still the alias, failed to
 * match the logged-out patterns, and returned the literal slug **"me"**.
 *
 * That is a false positive, and it fails in the worst direction: a logged-OUT
 * browser reported a healthy session. preflight passed, so a fire spent OSM
 * ground it could never use; `whoami.js --save` would have written
 * `expectProfile: "me"` and permanently neutered the real account check; and
 * the sender would have walked into login walls until the challenge breaker
 * tripped a 48h cooldown. Verified live 2026-08-03 against a Chrome parked on
 * linkedin.com/login — it reported "attached account: me".
 *
 * So: wait for the alias to actually resolve, and refuse to interpret it if it
 * never does.
 */
export async function whoami(page, timeout = 20000) {
  await page.goto('https://www.linkedin.com/in/me/', {
    waitUntil: 'domcontentloaded', timeout,
  });
  const deadline = Date.now() + timeout;
  let url = page.url();
  while (ME_ALIAS.test(url) && Date.now() < deadline) {
    await page.waitForTimeout(400);
    url = page.url();
  }
  if (ME_ALIAS.test(url)) return null;      // never resolved — do not guess
  if (LOGGED_OUT.test(url)) return null;
  return slugFromProfileUrl(url);
}

/**
 * Hard gate. Throws unless the attached session is the expected member.
 *
 * `expected` comes from config/account.json (or LINKEDIN_EXPECT_PROFILE). When
 * it is unset the check still RUNS and still requires a logged-in session — it
 * just cannot compare, so it reports who it found and lets the run continue.
 * Being logged in is never optional; knowing which account only becomes
 * enforceable once you have told it what to expect.
 */
export async function assertAccount(page, expected) {
  const actual = await whoami(page);
  if (!actual) {
    throw new Error(
      'the attached Chrome is NOT logged in to LinkedIn — refusing to send. '
      + 'Log in in that window, then re-run.');
  }
  const want = (expected ?? '').trim().toLowerCase();
  if (want && want !== actual) {
    throw new Error(
      `attached to the WRONG LinkedIn account: expected "${want}", found "${actual}". `
      + 'Refusing to send. Check which Chrome profile is on the debug port.');
  }
  return actual;
}

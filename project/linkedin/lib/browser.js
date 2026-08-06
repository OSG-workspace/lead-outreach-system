/*
 * ATTACH ONLY.
 *
 * We connect over CDP to a Chrome the human already has open and logged in, and
 * we use browser.contexts()[0] — the real profile. We never call
 * browser.newContext() (fresh, logged-out session), never launch, never
 * launchPersistentContext, and never browser.close() (that would kill the
 * human's browser). Pages we open, we close.
 *
 * Start Chrome for this with:
 *   /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
 *     --remote-debugging-port=9222 --user-data-dir="$HOME/chrome-linkedin"
 */
import { chromium } from 'playwright-core';

export const CDP_ENDPOINT = process.env.LINKEDIN_CDP_ENDPOINT || 'http://127.0.0.1:9222';

export async function attach(endpoint = CDP_ENDPOINT) {
  let browser;
  try {
    browser = await chromium.connectOverCDP(endpoint);
  } catch (e) {
    throw new Error(
      `could not attach to Chrome at ${endpoint}: ${e.message}. ` +
      'Start Chrome with --remote-debugging-port=9222 and log in to LinkedIn first. ' +
      'Do not let anything launch a browser for you.',
    );
  }
  const contexts = browser.contexts();
  if (contexts.length === 0) {
    await browser.browser?.()?.close?.().catch(() => {});
    throw new Error('attached but found no browser context — is this the right Chrome instance?');
  }
  return { browser, context: contexts[0] };
}

/** Detach without touching the human's browser. Explicitly NOT browser.close(). */
export async function detach(browser) {
  // Dropping the reference is all we may safely do: browser.close() on a
  // CDP-attached browser risks taking the human's Chrome with it, which is the
  // one thing this module must never do.
  //
  // THAT MEANS DETACHING DOES NOT END THE PROCESS. The old comment here claimed
  // "the process exit tears down the CDP socket", which is backwards — the open
  // socket is a live libuv handle, so it is precisely what KEEPS the process
  // alive. Every script that attached therefore ran to completion, printed its
  // result, and then hung forever. Measured 2026-08-03: preflight.js sat idle
  // 3+ minutes after logging "preflight OK".
  //
  // That was fatal in two places, not cosmetic:
  //   * run_fire.py runs `node linkedin/scripts/preflight.js` via a BLOCKING
  //     subprocess call, so a LinkedIn fire hung at Stage 0 before sourcing and
  //     never reached the pipeline at all;
  //   * daily.sh does `node scripts/preflight.js || exit 2`, so the daily drip
  //     hung on its first line every morning.
  //
  // So an entry point that attaches MUST terminate itself — use finish() below.
  void browser;
}

/**
 * End a script that attached to Chrome.
 *
 * Never return-and-hope: see detach() above. stdout is flushed first, because
 * process.exit() can otherwise truncate a pipe mid-write and the last line is
 * usually the result the caller is parsing.
 */
export function finish(code = 0) {
  const done = () => process.exit(code);
  if (process.stdout.writableLength === 0) return done();
  process.stdout.write('', done);
  setTimeout(done, 1000).unref();      // never wedge on a stuck pipe
}

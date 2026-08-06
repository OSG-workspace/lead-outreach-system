import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { paths } from '../../lib/paths.js';

/*
 * T15 — every script that attaches to Chrome must terminate itself.
 *
 * attach() opens a CDP WebSocket, which is a live libuv handle: while it is
 * open node will not exit on its own, and detach() deliberately does not close
 * it (browser.close() could take the human's Chrome with it). So a script that
 * merely RETURNS from main() runs to completion, prints its result, and then
 * hangs forever.
 *
 * Measured 2026-08-03 before the fix: preflight.js idle 3+ minutes after
 * logging "preflight OK"; three whoami.js processes alive 5, 9 and 15 minutes.
 *
 * This was not a tidiness problem. run_fire.py runs preflight.js through a
 * BLOCKING subprocess call, so every LinkedIn fire hung at Stage 0 before it
 * sourced anything; daily.sh gates on `node scripts/preflight.js || exit 2`, so
 * the daily drip hung on its first line; and sender.js returned without exiting
 * on success, leaving it alive forever holding paths.lock — which made the next
 * morning's run refuse as "lock busy".
 *
 * A static check, deliberately: the runtime version needs a live browser, and
 * this has to fail in plain `npm test` for whoever adds the next script.
 */

const ENTRY_POINTS = [
  'scripts/preflight.js',
  'scripts/whoami.js',
  'scripts/walk_companies.js',
  'scripts/sweep_acceptance.js',
  'scripts/withdraw_pending.js',
  'send/sender.js',
  'send/messenger.js',
];

test('T15 every entry point that attaches also terminates explicitly', () => {
  const offenders = [];
  for (const rel of ENTRY_POINTS) {
    const src = fs.readFileSync(path.join(paths.root, rel), 'utf8');
    if (!/\battach\s*\(/.test(src)) continue;          // does not touch a browser
    if (!/\bfinish\s*\(|process\.exit\s*\(/.test(src)) offenders.push(rel);
  }
  assert.deepEqual(offenders, [],
    `these attach to Chrome but never exit, so they hang forever: ${offenders.join(', ')}`);
});

test('T15 the attaching entry points are all still covered by this list', () => {
  // A new script that attaches must be added above, or it escapes the check.
  const missing = [];
  for (const dir of ['scripts', 'send', 'queue']) {
    for (const f of fs.readdirSync(path.join(paths.root, dir))) {
      if (!f.endsWith('.js')) continue;
      const rel = `${dir}/${f}`;
      const src = fs.readFileSync(path.join(paths.root, rel), 'utf8');
      if (/\battach\s*\(/.test(src) && !ENTRY_POINTS.includes(rel)) missing.push(rel);
    }
  }
  assert.deepEqual(missing, [],
    `new browser-attaching script(s) not covered by T15: ${missing.join(', ')}`);
});

test('T15 detach() does not pretend to end the process', () => {
  // The original comment claimed "the process exit tears down the CDP socket",
  // which inverts cause and effect and is what hid the hang for so long.
  const src = fs.readFileSync(path.join(paths.root, 'lib', 'browser.js'), 'utf8');
  assert.ok(/export function finish/.test(src), 'lib/browser.js must export finish()');
  assert.ok(!/dropping the reference is enough/.test(src),
    'the misleading detach() comment is back — re-read T15');
});

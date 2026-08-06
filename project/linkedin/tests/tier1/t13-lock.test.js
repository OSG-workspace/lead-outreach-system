import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { acquireLock, LockBusy } from '../../lib/lock.js';
import { paths } from '../../lib/paths.js';

/*
 * T13 — the overlap guard.
 *
 * This replaced `/usr/bin/flock`, which does not exist on macOS: daily.sh's
 * sender line exited 127, the caller only checked for 1, and so the entire send
 * side ran zero times while reporting nothing. These tests pin both halves —
 * that the lock actually excludes, and that it can never wedge shut.
 */

function tmpLock() {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'li-lock-')), 'run.lock');
}

test('T13 a second acquire while the holder is alive is refused', () => {
  const f = tmpLock();
  const release = acquireLock(f);
  assert.throws(() => acquireLock(f), LockBusy);
  release();
});

test('T13 an EMPTY lockfile is taken over, not a crash', () => {
  // The old code JSON.parse'd the lockfile unconditionally, so a 0-byte file
  // (exactly what the removed flock line left behind) threw a SyntaxError out
  // of the lock helper and killed the run with a FATAL naming a parse error.
  const f = tmpLock();
  fs.mkdirSync(path.dirname(f), { recursive: true });
  fs.writeFileSync(f, '');
  const release = acquireLock(f);
  assert.equal(JSON.parse(fs.readFileSync(f, 'utf8')).pid, process.pid);
  release();
});

test('T13 a stale lockfile from a dead process is taken over', () => {
  const f = tmpLock();
  fs.mkdirSync(path.dirname(f), { recursive: true });
  // PID 2^22 is above every real pid on macOS/Linux, so it cannot be running.
  fs.writeFileSync(f, JSON.stringify({ pid: 4194304, at: '2020-01-01T00:00:00Z' }));
  const release = acquireLock(f);
  assert.equal(JSON.parse(fs.readFileSync(f, 'utf8')).pid, process.pid);
  release();
});

test('T13 the lock is released even when the process exits without unwinding', () => {
  // The messenger's old lock was never released on its identity-failure
  // process.exit(2), so one logged-out morning wedged it permanently.
  const f = tmpLock();
  let code = 0;
  try {
    execFileSync(process.execPath, [
      '--input-type=module', '-e',
      `import { acquireLock } from ${JSON.stringify(path.join(paths.root, 'lib', 'lock.js'))};`
      + `acquireLock(${JSON.stringify(f)}); process.exit(2);`,
    ], { stdio: 'pipe' });
  } catch (e) {
    code = e.status;                    // a non-zero exit is the point of the test
  }
  assert.equal(code, 2);
  assert.equal(fs.existsSync(f), false, 'lockfile survived a hard process.exit()');
});

test('T13 release does not delete a lock another process has taken over', () => {
  const f = tmpLock();
  const release = acquireLock(f);
  fs.writeFileSync(f, JSON.stringify({ pid: 999999, at: 'later' }));  // someone else
  release();
  assert.equal(fs.existsSync(f), true);
});

test('T13 daily.sh does not depend on flock(1)', () => {
  // macOS ships no flock. Reintroducing it silently disables the whole send side.
  const daily = fs.readFileSync(path.join(paths.root, 'scripts', 'daily.sh'), 'utf8');
  const live = daily.split('\n').filter((l) => !l.trim().startsWith('#')).join('\n');
  assert.ok(!/\bflock\b/.test(live), 'daily.sh must not call flock');
});

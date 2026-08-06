import fs from 'node:fs';
import path from 'node:path';

/*
 * THE OVERLAP GUARD, and why it is not flock(1).
 *
 * Both senders pace themselves across the whole 09:15-17:30 window, so a second
 * run started while the first is still sleeping between actions would double the
 * day's volume — the one thing the ramp exists to prevent. daily.sh used to wrap
 * them in `/usr/bin/flock`, which does not exist on macOS: the line exited 127
 * and, since the caller only checked for 1, the failure was swallowed and the
 * senders were never invoked at all. The whole send side was silently dead.
 *
 * So the lock lives here instead: a PID-stamped file, checked for liveness, with
 * no external dependency. Three failure modes it has to survive, because all
 * three happen on a laptop:
 *
 *   - holder still running   -> refuse (exit 3), that is the point
 *   - holder crashed / was killed (stale PID) -> take it over, do not stall forever
 *   - file exists but is EMPTY or corrupt (a truncated write, or a leftover from
 *     the old flock line, which created a 0-byte file) -> take it over. The old
 *     code JSON.parse'd it unconditionally, so an empty lockfile threw a
 *     SyntaxError out of the lock helper and killed the run with a FATAL that
 *     named a parse error rather than a lock.
 */

export const LOCK_BUSY_EXIT = 3;

export class LockBusy extends Error {}

/**
 * Take `file` exclusively. Returns a release() that is also registered on exit,
 * so no code path can leak the lock — including the process.exit() calls the
 * senders make on an identity-check failure, which used to leave the messenger
 * permanently unable to start.
 */
export function acquireLock(file) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const stamp = () => JSON.stringify({ pid: process.pid, at: new Date().toISOString() });

  try {
    fs.writeFileSync(file, stamp(), { flag: 'wx' });
  } catch (e) {
    if (e.code !== 'EEXIST') throw e;
    let held = null;
    try {
      held = JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch {
      held = null;                       // empty or corrupt -> treat as stale
    }
    if (held && Number.isInteger(held.pid) && alive(held.pid)) {
      throw new LockBusy(`another run is active (pid ${held.pid}, since ${held.at ?? '?'})`);
    }
    fs.writeFileSync(file, stamp());     // stale or unreadable: take it over
  }

  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    try {
      // Only remove it if it is still OURS — a takeover by another process
      // must not have its lock deleted by our exit handler.
      const cur = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (cur.pid !== process.pid) return;
    } catch { /* unreadable: fall through and clear it */ }
    try { fs.unlinkSync(file); } catch { /* already gone */ }
  };
  process.on('exit', release);
  return release;
}

function alive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code !== 'ESRCH';           // EPERM = running, owned by someone else
  }
}

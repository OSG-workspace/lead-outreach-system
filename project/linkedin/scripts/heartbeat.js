#!/usr/bin/env node
/*
 * Silent failure is the main risk on this channel: a broken cron, a stale queue
 * or a logged-out browser all look exactly like "a quiet day".
 * If a scheduled weekday produced zero sends and no breaker fired, shout.
 */
import fs from 'node:fs';
import { paths } from '../lib/paths.js';
import { loadLimits, isSendDay, dateKeyInTz } from '../lib/limits.js';
import { loadState, invitesToday } from '../lib/state.js';
import { cooldownActive } from '../lib/breakers.js';
import { alert, readAlerts } from '../lib/alerts.js';

export function heartbeatCheck({ limits, state, alerts, queue, backlog, now }) {
  const today = dateKeyInTz(now, limits.timezone);
  if (!isSendDay(limits, now)) return { ok: true, reason: 'not a send day' };
  if (cooldownActive(state, now.getTime())) return { ok: true, reason: 'cooldown active (breaker fired)' };

  const sent = invitesToday(state, limits.timezone, now);
  if (sent > 0) return { ok: true, reason: `${sent} invite(s) sent today` };

  // NO SUPPLY IS NOT A FAULT. Between campaigns the backlog legitimately runs
  // dry, and there is nothing for the generator to queue. Reporting that as a
  // silent-failure ALERT every single weekday is worse than saying nothing: an
  // alarm that cries wolf daily is one nobody reads on the day it is real.
  if (!backlog || backlog.length === 0) {
    return { ok: true, reason: 'no invite supply left — fire a campaign to refill the backlog' };
  }

  const breakerToday = alerts.some(
    (a) => (a.level === 'HALT' || a.level === 'FATAL') && dateKeyInTz(new Date(a.at), limits.timezone) === today,
  );
  if (breakerToday) return { ok: true, reason: 'zero sends but a breaker fired and was logged' };

  if (!queue) return { ok: false, reason: 'zero sends today and no queue.json exists — generator never ran' };
  if (queue.date !== today) return { ok: false, reason: `zero sends today and queue.json is stale (${queue.date})` };
  if (queue.entries.length === 0) return { ok: false, reason: 'zero sends today and the queue is empty — no eligible leads?' };
  return { ok: false, reason: `zero sends today with ${queue.entries.length} queued and no breaker — sender silently failed` };
}

function main() {
  const limits = loadLimits();
  const state = loadState(paths.state);
  const alerts = readAlerts(paths.alerts);
  const queue = fs.existsSync(paths.queue) ? JSON.parse(fs.readFileSync(paths.queue, 'utf8')) : null;
  const backlog = fs.existsSync(paths.backlog)
    ? JSON.parse(fs.readFileSync(paths.backlog, 'utf8')) : null;
  const res = heartbeatCheck({ limits, state, alerts, queue, backlog, now: new Date() });
  if (res.ok) { console.log(`[heartbeat] ok — ${res.reason}`); return; }
  alert(paths.alerts, 'ALERT', `heartbeat: ${res.reason}`);
  process.exit(1);
}

if (import.meta.url === `file://${process.argv[1]}`) main();

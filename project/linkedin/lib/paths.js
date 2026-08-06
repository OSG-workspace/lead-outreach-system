import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

// LINKEDIN_STATE_DIR redirects everything MUTABLE (state, queue, alerts, lock).
// It exists because a test that spawns a real sender to prove the config guard
// works was writing its deliberately-broken-config FATAL straight into the
// operational state/alerts.log — 19 of the 20 lines in it were test noise. That
// log is not decoration: heartbeat.js reads it to decide whether a zero-send day
// had a breaker behind it, so test lines can mask a genuine silent failure.
const STATE_DIR = process.env.LINKEDIN_STATE_DIR || path.join(ROOT, 'state');

export const paths = {
  root: ROOT,
  // LINKEDIN_LIMITS_FILE lets a test point the config guard at a fixture
  // without ever mutating the shipped limits.json.
  limits: process.env.LINKEDIN_LIMITS_FILE || path.join(ROOT, 'config', 'limits.json'),
  campaign: path.join(ROOT, 'config', 'campaign.json'),
  queue: path.join(STATE_DIR, 'queue.json'),
  state: path.join(STATE_DIR, 'state.json'),
  review: path.join(STATE_DIR, 'review-required.json'),
  alerts: path.join(STATE_DIR, 'alerts.log'),
  lock: path.join(STATE_DIR, 'run.lock'),
  messengerLock: path.join(STATE_DIR, '.messenger.lock'),
  // Weeks of invite supply, written by tools/scripts/linkedin_queue.py at fire
  // time and drained a ramp-cap a day by generate.js --from-backlog.
  backlog: path.join(STATE_DIR, 'backlog.json'),
  dmQueue: path.join(STATE_DIR, 'dm-queue.json'),
  debug: path.join(ROOT, 'debug'),
  // Shared, cross-channel suppression. Authoritative for BOTH email and LinkedIn.
  suppression: path.resolve(ROOT, '..', 'vault', 'lead-outreach', 'suppression.md'),
  bounces: path.resolve(ROOT, '..', 'vault', 'lead-outreach', 'bounce-list.md'),
};

// Everything under state/ is per-run mutable data; tests point this at a temp dir.
export function withRoot(newRoot) {
  return {
    root: newRoot,
    limits: path.join(newRoot, 'config', 'limits.json'),
    campaign: path.join(newRoot, 'config', 'campaign.json'),
    queue: path.join(newRoot, 'state', 'queue.json'),
    state: path.join(newRoot, 'state', 'state.json'),
    review: path.join(newRoot, 'state', 'review-required.json'),
    alerts: path.join(newRoot, 'state', 'alerts.log'),
    lock: path.join(newRoot, 'state', 'run.lock'),
    debug: path.join(newRoot, 'debug'),
    suppression: path.join(newRoot, 'suppression.md'),
    bounces: path.join(newRoot, 'bounce-list.md'),
  };
}

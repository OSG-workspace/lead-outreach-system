import fs from 'node:fs';
import path from 'node:path';
import { paths } from './paths.js';

export const ACCOUNT_FILE = path.join(paths.root, 'config', 'account.json');

/**
 * The LinkedIn profile slug this module is allowed to send as, or null.
 *
 * Recorded once by `node scripts/whoami.js --save`, then enforced by the
 * senders before any action. Kept in config/ rather than state/ because it is
 * an operator decision, not run data — it should survive a state wipe.
 */
export function loadAccount() {
  const env = (process.env.LINKEDIN_EXPECT_PROFILE ?? '').trim().toLowerCase();
  if (env) return env;
  try {
    const raw = JSON.parse(fs.readFileSync(ACCOUNT_FILE, 'utf8'));
    return (raw.expectProfile ?? '').trim().toLowerCase() || null;
  } catch {
    return null;
  }
}

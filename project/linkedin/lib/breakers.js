import { CHALLENGE_PATTERNS, CHALLENGE_TEXT } from './selectors.js';

export const COOLDOWN_MS = 48 * 60 * 60 * 1000;

export class HardStop extends Error {
  constructor(message, kind) { super(message); this.kind = kind; }
}

/**
 * Strip markup so phrase matching sees what a HUMAN sees.
 *
 * Raw HTML is full of things that are not page content: script bodies, iframe
 * and script `src` attributes, JSON payloads, CSS class names. Matching against
 * it means any vendor string anywhere on the page can trip a 48h cooldown —
 * which is exactly what happened: LinkedIn embeds Google reCAPTCHA Enterprise
 * on ordinary pages, the old code searched the raw HTML for "captcha", and a
 * healthy /feed/ read as a challenge (measured live, 2026-08-03).
 *
 * Scripts and styles are removed wholesale, then all tags — which also removes
 * every attribute value, so an iframe pointing at recaptcha cannot match.
 */
export function visibleText(html) {
  return String(html || '')
    .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, ' ')
    .replace(/<noscript\b[^>]*>[\s\S]*?<\/noscript>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .toLowerCase();
}

/**
 * Pure breaker check over what a navigation produced. Returns a reason string
 * when the run must hard-stop for 48h, or null.
 *
 * Order matters: status and URL are the RELIABLE signals and are checked first.
 * Text is corroboration only, and is matched against visible text, never raw
 * markup. Pass `text` directly (e.g. from body.innerText) when you have it;
 * otherwise `html` is stripped here.
 */
export function detectChallenge({ status, url, html, text }) {
  if (status === 429) return 'HTTP 429 (rate limited)';
  if (status === 999) return 'HTTP 999 (LinkedIn block)';
  if (typeof status === 'number' && status >= 500) return `HTTP ${status}`;
  for (const re of CHALLENGE_PATTERNS) {
    if (re.test(url || '')) return `challenge/redirect URL: ${url}`;
  }
  const body = text !== undefined ? String(text).toLowerCase() : visibleText(html);
  for (const t of CHALLENGE_TEXT) {
    if (body.includes(t)) return `challenge page text: "${t}"`;
  }
  return null;
}

/** Acceptance-floor breaker: stop NEW invites (DMs may continue). */
export function acceptanceBreach(limits, { rate, sample }) {
  if (rate === null) return null;
  if (sample < limits.acceptanceFloorSampleSize) return null;
  if (rate >= limits.acceptanceFloorPct) return null;
  return `trailing-${sample} acceptance ${rate.toFixed(1)}% is below floor ${limits.acceptanceFloorPct}%`;
}

export function weeklyBreach(limits, weekCount) {
  return weekCount >= limits.weeklyMax
    ? `weeklyMax reached (${weekCount}/${limits.weeklyMax}) — waiting for the rolling 7-day window to clear`
    : null;
}

export function cooldownActive(state, now = Date.now()) {
  if (!state.cooldownUntil) return null;
  const until = Date.parse(state.cooldownUntil);
  return Number.isFinite(until) && until > now ? state.cooldownUntil : null;
}

export function startCooldown(state, now = Date.now()) {
  state.cooldownUntil = new Date(now + COOLDOWN_MS).toISOString();
  return state.cooldownUntil;
}

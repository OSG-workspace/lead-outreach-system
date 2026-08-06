import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { validateLimits } from '../../lib/limits.js';
import { paths } from '../../lib/paths.js';
import {
  detectChallenge, acceptanceBreach, weeklyBreach, startCooldown, cooldownActive, COOLDOWN_MS,
} from '../../lib/breakers.js';
import { loadState, markSent, markStatus, trailingAcceptance, invitesInRollingWeek } from '../../lib/state.js';

const LIMITS = validateLimits(JSON.parse(fs.readFileSync(paths.limits, 'utf8')));
const PROFILE = 'https://www.linkedin.com/in/someone/';

// T5 — injected 429 / checkpoint / captcha => 48h halt
test('T5 HTTP 429 trips the breaker', () => {
  assert.match(detectChallenge({ status: 429, url: PROFILE, html: '<html></html>' }), /429/);
});

test('T5 HTTP 999 trips the breaker', () => {
  assert.match(detectChallenge({ status: 999, url: PROFILE, html: '' }), /999/);
});

test('T5 a /checkpoint/ redirect trips the breaker', () => {
  const reason = detectChallenge({
    status: 200,
    url: 'https://www.linkedin.com/checkpoint/challenge/AgH...',
    html: '<html><body>Verify</body></html>',
  });
  assert.match(reason, /challenge\/redirect URL/);
});

test('T5 a CAPTCHA / challenge page body trips the breaker', () => {
  const html = '<html><body><h1>Let’s do a quick security check</h1><div id="captcha"></div></body></html>';
  assert.ok(detectChallenge({ status: 200, url: PROFILE, html }));
});

test('T5 a login redirect trips the breaker', () => {
  assert.ok(detectChallenge({ status: 200, url: 'https://www.linkedin.com/uas/login?session_redirect=x', html: '' }));
});

test('T5 a normal profile does NOT trip the breaker', () => {
  const html = '<html><body><main><button aria-label="Invite Dana to connect"></button></main></body></html>';
  assert.equal(detectChallenge({ status: 200, url: PROFILE, html }), null);
});

test('T5 tripping the breaker sets a 48h cooldown that blocks the next run', () => {
  const state = loadState('/nonexistent/state.json');
  const now = Date.UTC(2026, 6, 27, 10, 0, 0);
  const until = startCooldown(state, now);
  assert.equal(Date.parse(until) - now, COOLDOWN_MS);
  assert.ok(cooldownActive(state, now + 47 * 3600 * 1000), 'still cooling at +47h');
  assert.equal(cooldownActive(state, now + 49 * 3600 * 1000), null, 'clear at +49h');
});

// Acceptance-floor breaker
test('T5 acceptance below the floor stops new invites once the sample is full', () => {
  const state = loadState('/nonexistent/state.json');
  const base = Date.UTC(2026, 6, 1);
  for (let i = 0; i < 100; i++) {
    const url = `https://www.linkedin.com/in/p${i}/`;
    markSent(state, url, LIMITS.timezone, new Date(base + i * 3600000));
    if (i < 10) markStatus(state, url, 'accepted', { sentAt: state.leads[url].sentAt }); // 10%
  }
  const t = trailingAcceptance(state, LIMITS.acceptanceFloorSampleSize);
  assert.equal(t.sample, 100);
  assert.match(acceptanceBreach(LIMITS, t), /below floor/);
});

test('T5 acceptance breaker stays quiet below the sample size', () => {
  const state = loadState('/nonexistent/state.json');
  for (let i = 0; i < 20; i++) markSent(state, `https://www.linkedin.com/in/q${i}/`, LIMITS.timezone, new Date());
  assert.equal(acceptanceBreach(LIMITS, trailingAcceptance(state, LIMITS.acceptanceFloorSampleSize)), null);
});

// Weekly breaker
test('T5 weeklyMax halts once the rolling 7-day window is full', () => {
  const state = loadState('/nonexistent/state.json');
  const now = new Date('2026-07-27T10:00:00Z');
  for (let d = 0; d < 7; d++) {
    for (let i = 0; i < 13; i++) {
      markSent(state, `https://www.linkedin.com/in/w${d}-${i}/`, LIMITS.timezone, new Date(now.getTime() - d * 86400000));
    }
  }
  const count = invitesInRollingWeek(state, LIMITS.timezone, now);
  assert.equal(count, 91);
  assert.match(weeklyBreach(LIMITS, count), /weeklyMax reached/);
  assert.equal(weeklyBreach(LIMITS, 89), null);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { detectChallenge, visibleText } from '../../lib/breakers.js';

/*
 * T16 — the challenge breaker must not fire on a healthy page.
 *
 * Found live on 2026-08-03: tier2's T10 loaded an ordinary /feed/ and the
 * breaker reported `challenge page text: "captcha"`. LinkedIn embeds Google
 * reCAPTCHA Enterprise on normal pages, and the old detector lowercased the
 * WHOLE raw HTML and substring-matched the bare word "captcha".
 *
 * The consequence was not a noisy log. gotoChecked() throws HardStop on a
 * positive, so the FIRST profile the sender opened would have written a 48-hour
 * cooldown and halted — and preflight refuses to start while a cooldown is
 * active, so the channel would have bricked itself for two days on its first
 * real action, reporting a LinkedIn block that never happened.
 *
 * A false NEGATIVE here costs one bad page load. A false POSITIVE costs two
 * days and looks like an account restriction. These tests pin both directions.
 */

// Trimmed from what LinkedIn actually serves on a logged-in feed.
const HEALTHY_FEED = `
<!DOCTYPE html><html><head>
  <script src="https://www.google.com/recaptcha/enterprise.js?render=6LcIy"></script>
  <style>.captcha-holder{display:none}</style>
</head><body>
  <main><div class="feed-shared-update-v2">Ahmed posted about Palm Jumeirah listings</div></main>
  <iframe src="https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=6LcIy"></iframe>
</body></html>`;

const REAL_CHALLENGE = `
<!DOCTYPE html><html><body>
  <h1>Let's do a quick security check</h1>
  <p>We've detected unusual activity from your account.</p>
</body></html>`;

test('T16 an ordinary feed with an embedded reCAPTCHA does NOT trip the breaker', () => {
  assert.equal(
    detectChallenge({ status: 200, url: 'https://www.linkedin.com/feed/', html: HEALTHY_FEED }),
    null,
    'a healthy page tripped the breaker — this writes a 48h cooldown and bricks the channel',
  );
});

test('T16 markup stripping removes script bodies, styles and attribute values', () => {
  const t = visibleText(HEALTHY_FEED);
  assert.ok(!t.includes('captcha'), `"captcha" survived stripping: ${t}`);
  assert.ok(t.includes('palm jumeirah'), 'real page text must survive stripping');
});

test('T16 a genuine challenge page still trips it', () => {
  const r = detectChallenge({ status: 200, url: 'https://www.linkedin.com/feed/', html: REAL_CHALLENGE });
  assert.match(String(r), /quick security check|unusual activity/);
});

test('T16 the reliable signals are unaffected', () => {
  assert.match(String(detectChallenge({ status: 429, url: 'x' })), /429/);
  assert.match(String(detectChallenge({ status: 999, url: 'x' })), /999/);
  assert.match(
    String(detectChallenge({ status: 200, url: 'https://www.linkedin.com/checkpoint/challenge/' })),
    /challenge\/redirect URL/);
  assert.match(
    String(detectChallenge({ status: 200, url: 'https://www.linkedin.com/authwall?x=1' })),
    /challenge\/redirect URL/);
});

test('T16 no CHALLENGE_TEXT entry is a bare single word', async () => {
  // A single word can appear in any vendor blob or unrelated copy. Whole
  // phrases are what a human reads on the interstitial.
  const { CHALLENGE_TEXT } = await import('../../lib/selectors.js');
  const bare = CHALLENGE_TEXT.filter((t) => !t.trim().includes(' '));
  assert.deepEqual(bare, [], `bare single-word challenge markers are too broad: ${bare.join(', ')}`);
});

test('T16 explicit visible text wins over html when supplied', () => {
  const r = detectChallenge({
    status: 200, url: 'https://www.linkedin.com/feed/',
    html: HEALTHY_FEED, text: 'We have detected unusual activity',
  });
  assert.match(String(r), /unusual activity/);
});

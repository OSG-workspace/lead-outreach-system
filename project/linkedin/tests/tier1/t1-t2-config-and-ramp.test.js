import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { validateLimits, capForDay, LimitsError } from '../../lib/limits.js';
import { paths } from '../../lib/paths.js';

const BASE = JSON.parse(fs.readFileSync(paths.limits, 'utf8'));

// T1 — config guard: daily cap > absoluteDailyMax => refuses to start
test('T1 config guard rejects a ramp step above absoluteDailyMax', () => {
  const bad = structuredClone(BASE);
  bad.ramp[bad.ramp.length - 1].invitesPerDay = 25; // > absoluteDailyMax 20
  assert.throws(() => validateLimits(bad), LimitsError);
});

test('T1 the shipped limits.json is valid', () => {
  assert.doesNotThrow(() => validateLimits(structuredClone(BASE)));
});

test('T1 sender.js exits non-zero on a bad config without attaching a browser', () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'li-t1-'));
  const badFile = path.join(tmp, 'limits.json');
  const bad = structuredClone(BASE);
  bad.ramp[0].invitesPerDay = 99; // > absoluteDailyMax
  fs.writeFileSync(badFile, JSON.stringify(bad));
  // LINKEDIN_STATE_DIR: this spawns a REAL sender, whose config guard writes a
  // FATAL alert. Without the redirect that line lands in the operational
  // state/alerts.log, which heartbeat.js reads to decide whether a zero-send day
  // had a breaker behind it — so every `npm test` was quietly degrading the one
  // signal that catches a silent failure.
  fs.mkdirSync(path.join(tmp, 'state'), { recursive: true });
  try {
    let code = 0;
    try {
      execFileSync(process.execPath, [path.join(paths.root, 'send', 'sender.js')], {
        encoding: 'utf8',
        stdio: 'pipe',
        env: {
          ...process.env,
          LINKEDIN_LIMITS_FILE: badFile,
          LINKEDIN_STATE_DIR: path.join(tmp, 'state'),
        },
      });
    } catch (e) {
      code = e.status;
    }
    assert.equal(code, 2, 'sender must refuse to start with exit code 2');
    assert.ok(
      !fs.readFileSync(paths.alerts, 'utf8').includes('invitesPerDay=99'),
      'the test sender must not write into the operational alerts.log',
    );
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

// T2 — ramp schedule, boundaries included
test('T2 ramp returns the expected cap on every day incl. boundaries', () => {
  const l = validateLimits(structuredClone(BASE));
  const expected = {
    1: 8, 4: 8, 5: 8,        // throughDay 5 inclusive
    6: 12, 10: 12,           // throughDay 10 inclusive
    11: 15, 15: 15,          // throughDay 15 inclusive
    16: 18, 40: 18, 400: 18, // tail
  };
  for (const [day, cap] of Object.entries(expected)) {
    assert.equal(capForDay(l, Number(day)), cap, `day ${day}`);
  }
  assert.throws(() => capForDay(l, 0), LimitsError);
});

test('T2 cap is always clamped by absoluteDailyMax', () => {
  const l = validateLimits(structuredClone(BASE));
  for (let d = 1; d <= 60; d++) assert.ok(capForDay(l, d) <= l.absoluteDailyMax);
});

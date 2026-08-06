import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { validateLimits, minutesOfDay, minutesOfDayInTz, capForDay } from '../../lib/limits.js';
import { paths } from '../../lib/paths.js';
import { buildSchedule, intervalBounds, nextGapMinutes } from '../../lib/pacing.js';

const LIMITS = validateLimits(JSON.parse(fs.readFileSync(paths.limits, 'utf8')));
const WIN_START = minutesOfDay(LIMITS.sendWindow.start);
const WIN_END = minutesOfDay(LIMITS.sendWindow.end);
const WIN_LEN = WIN_END - WIN_START;

// T6 — 1000 simulated days
test('T6 1000 simulated days: gaps in bounds, never capped out, spread across the window', () => {
  const allGaps = [];
  const lastFractions = [];
  const firstFractions = [];
  const cap = capForDay(LIMITS, 99); // steady state, 18/day

  for (let day = 0; day < 1000; day++) {
    const ref = new Date(Date.UTC(2026, 6, 27, 6, 0, 0) + day * 86400000);
    const times = buildSchedule(LIMITS, cap, ref);

    assert.equal(times.length, cap, 'schedule never exceeds the cap');
    assert.ok(times.length <= LIMITS.absoluteDailyMax);

    const mins = times.map((t) => minutesOfDayInTz(t, LIMITS.timezone));
    for (const m of mins) {
      assert.ok(m >= WIN_START && m <= WIN_END, `slot ${m} outside window on day ${day}`);
    }
    for (let i = 1; i < mins.length; i++) {
      assert.ok(mins[i] > mins[i - 1], 'schedule must be strictly increasing');
    }

    const bounds = intervalBounds(LIMITS, cap);
    const gaps = [];
    for (let i = 1; i < times.length; i++) {
      gaps.push((times[i] - times[i - 1]) / 60000);
    }
    gaps.forEach((g, i) => {
      // 1-minute tolerance: schedule slots are rounded to whole minutes.
      assert.ok(g >= bounds[i].lo - 1, `gap ${g} below min ${bounds[i].lo} (day ${day}, i ${i})`);
      assert.ok(g <= bounds[i].hi + 1, `gap ${g} above max ${bounds[i].hi} (day ${day}, i ${i})`);
    });
    // Never a fixed interval. Slots land on whole milliseconds, so two gaps in a
    // day can coincide by chance (~1 day in 8); a repeat of three is not chance.
    const perDay = gaps.map((g) => g.toFixed(4));
    const counts = new Map();
    for (const g of perDay) counts.set(g, (counts.get(g) || 0) + 1);
    assert.ok(Math.max(...counts.values()) <= 2, `day ${day} repeated one gap length 3+ times`);
    assert.ok(new Set(perDay).size >= perDay.length - 1, `day ${day} looks like a fixed interval`);

    allGaps.push(...gaps);
    firstFractions.push((mins[0] - WIN_START) / WIN_LEN);
    lastFractions.push((mins[mins.length - 1] - WIN_START) / WIN_LEN);
  }

  // Across the whole simulation the gap lengths are essentially all distinct.
  const rounded = allGaps.map((g) => g.toFixed(4));
  assert.ok(
    new Set(rounded).size > rounded.length * 0.9,
    `only ${new Set(rounded).size}/${rounded.length} distinct gap lengths — pacing is not re-randomised`,
  );

  // Spread: the day reaches deep into the afternoon rather than front-loading.
  const avgLast = lastFractions.reduce((a, b) => a + b, 0) / lastFractions.length;
  assert.ok(avgLast > 0.8, `last action averages only ${(avgLast * 100).toFixed(1)}% into the window`);
  assert.ok(Math.min(...lastFractions) > 0.6, 'some day finished far too early');
  const avgFirst = firstFractions.reduce((a, b) => a + b, 0) / firstFractions.length;
  assert.ok(avgFirst < 0.1, 'first action should still start near the top of the window');
});

test('T6 burst pauses are applied on every Nth boundary', () => {
  const bounds = intervalBounds(LIMITS, 10);
  const n = LIMITS.burstPause.afterActions;
  bounds.forEach((b, idx) => {
    const i = idx + 1;
    if (i % n === 0) {
      assert.equal(b.lo, LIMITS.gapMinutes.min + LIMITS.burstPause.pauseMinutes.min);
      assert.equal(b.hi, LIMITS.gapMinutes.max + LIMITS.burstPause.pauseMinutes.max);
    } else {
      assert.equal(b.lo, LIMITS.gapMinutes.min);
      assert.equal(b.hi, LIMITS.gapMinutes.max);
    }
  });
});

test('T6 run-time gaps are re-randomised, never chained or fixed', () => {
  const seen = new Set();
  for (let i = 0; i < 5000; i++) {
    const g = nextGapMinutes(LIMITS, (i % 5) + 1);
    const isBurst = ((i % 5) + 1) % LIMITS.burstPause.afterActions === 0;
    const lo = LIMITS.gapMinutes.min + (isBurst ? LIMITS.burstPause.pauseMinutes.min : 0);
    const hi = LIMITS.gapMinutes.max + (isBurst ? LIMITS.burstPause.pauseMinutes.max : 0);
    assert.ok(g >= lo && g <= hi);
    seen.add(g);
  }
  // Full float resolution here: nothing rounds these, so a repeat would mean a
  // chained or fixed interval, not a coincidence.
  assert.equal(seen.size, 5000);
});

test('T6 a cap that cannot be paced inside the window is refused, not squeezed', () => {
  assert.throws(() => buildSchedule(LIMITS, 60, new Date()), /too short/);
});

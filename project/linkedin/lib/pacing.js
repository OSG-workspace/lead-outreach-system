import { minutesOfDay, dateAtMinutesInTz } from './limits.js';

/** Continuous uniform sample in [min, max]. Continuous on purpose: two gaps are never identical. */
export function uniform(min, max, rng = Math.random) {
  return min + (max - min) * rng();
}

export function randomInt(min, max, rng = Math.random) {
  return Math.floor(uniform(min, max + 1, rng));
}

/**
 * Per-interval bounds for `count` actions: count-1 gaps, where every
 * burstPause.afterActions-th boundary also carries a long pause.
 */
export function intervalBounds(limits, count) {
  const out = [];
  for (let i = 1; i < count; i++) {
    let lo = limits.gapMinutes.min;
    let hi = limits.gapMinutes.max;
    if (limits.burstPause?.afterActions && i % limits.burstPause.afterActions === 0) {
      lo += limits.burstPause.pauseMinutes.min;
      hi += limits.burstPause.pauseMinutes.max;
    }
    out.push({ lo, hi });
  }
  return out;
}

/**
 * Randomly distribute `target` total minutes across intervals, keeping every
 * interval inside its own bounds. Weighted-headroom fill with clamping, so the
 * schedule stretches across the whole window instead of front-loading it.
 */
export function fitIntervals(bounds, target, rng = Math.random) {
  const lows = bounds.map((b) => b.lo);
  const sumLow = lows.reduce((a, b) => a + b, 0);
  const sumHigh = bounds.reduce((a, b) => a + b.hi, 0);
  const want = Math.min(Math.max(target, sumLow), sumHigh);

  const x = lows.slice();
  let slack = want - sumLow;
  let headroom = bounds.map((b, i) => b.hi - x[i]);

  for (let pass = 0; pass < 12 && slack > 1e-9; pass++) {
    const weights = bounds.map((_, i) => (headroom[i] > 1e-9 ? rng() + 1e-6 : 0));
    const wsum = weights.reduce((a, b) => a + b, 0);
    if (wsum <= 0) break;
    let given = 0;
    for (let i = 0; i < x.length; i++) {
      const share = Math.min(slack * (weights[i] / wsum), headroom[i]);
      x[i] += share;
      headroom[i] -= share;
      given += share;
    }
    slack -= given;
    if (given < 1e-9) break;
  }

  // De-tie: the clamping fill can park several intervals exactly on their upper
  // bound, which would show up as a repeated gap length. Shuffle small amounts
  // between random pairs (total preserved, bounds respected) so no two match.
  for (let k = 0; k < x.length * 8; k++) {
    const i = Math.floor(rng() * x.length);
    const j = Math.floor(rng() * x.length);
    if (i === j) continue;
    const room = Math.min(x[i] - bounds[i].lo, bounds[j].hi - x[j]);
    if (room <= 1e-6) continue;
    const d = rng() * Math.min(room, 2);
    x[i] -= d;
    x[j] += d;
  }
  return x;
}

/**
 * Build today's send schedule: `count` Dates inside the send window, gaps
 * re-randomised, burst pauses honoured, spread across the full window.
 */
export function buildSchedule(limits, count, refDate, rng = Math.random) {
  if (count <= 0) return [];
  const winStart = minutesOfDay(limits.sendWindow.start);
  const winEnd = minutesOfDay(limits.sendWindow.end);
  const winLen = winEnd - winStart;

  const bounds = intervalBounds(limits, count);
  const sumLow = bounds.reduce((a, b) => a + b.lo, 0);
  if (sumLow > winLen) {
    throw new Error(
      `send window (${winLen}min) is too short for ${count} actions at minimum pacing (${Math.round(sumLow)}min)`,
    );
  }

  // Random head offset and tail slack so the run neither starts nor ends on the dot,
  // while still reaching deep into the afternoon.
  const head = uniform(0, Math.min(20, winLen - sumLow), rng);
  const maxSpan = winLen - head;
  const targetSpan = uniform(Math.max(sumLow, maxSpan * 0.85), maxSpan, rng);

  const gaps = fitIntervals(bounds, targetSpan, rng);

  const offsets = [head];
  for (const g of gaps) offsets.push(offsets[offsets.length - 1] + g);

  return offsets.map((o) => {
    const mins = winStart + o;
    const base = dateAtMinutesInTz(refDate, limits.timezone, Math.floor(mins));
    return new Date(base.getTime() + Math.round((mins % 1) * 60000));
  });
}

/** Gap in minutes to wait before action #index (1-based), re-randomised at call time. */
export function nextGapMinutes(limits, index, rng = Math.random) {
  let gap = uniform(limits.gapMinutes.min, limits.gapMinutes.max, rng);
  if (limits.burstPause?.afterActions && index > 0 && index % limits.burstPause.afterActions === 0) {
    gap += uniform(limits.burstPause.pauseMinutes.min, limits.burstPause.pauseMinutes.max, rng);
  }
  return gap;
}

export function dwellMs(limits, rng = Math.random) {
  return Math.round(uniform(limits.profileDwellSeconds.min, limits.profileDwellSeconds.max, rng) * 1000);
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

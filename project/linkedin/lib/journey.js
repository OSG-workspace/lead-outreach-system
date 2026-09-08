/*
 * THE JOURNEY ENGINE — pure, deterministic, no I/O, no browser, no LLM.
 *
 * WHAT PROBLEM THIS SOLVES
 * The send side used to be stateless in time: sweep found an acceptance, a DM
 * went out, and that was the end of the relationship. Anything after the first
 * message — waiting two days, following up with a video, giving up and telling
 * a human — lived in nobody's head but David's. This file makes the schedule a
 * data structure.
 *
 * THE ONE IDEA
 * A lead's next action is DERIVED, never stored. Each completed step stamps a
 * timestamp in `lead.journey.history`; a step is due when
 *
 *     now >= history[step.after.from] + step.after.days
 *
 * Nothing precomputes a due date, so editing config/journey.json reschedules
 * every lead already mid-journey, and a tick that never ran (laptop shut, a
 * week off) simply finds the step overdue rather than lost. Ticks are therefore
 * idempotent and skippable, which is the property a cloud schedule needs.
 *
 * ORDER IS ABSOLUTE. Step N is only ever considered once step N-1 is stamped,
 * so a lead cannot receive the video before the outreach even if the outreach
 * failed to send for a week.
 */

const DAY_MS = 86400000;

export function loadJourney(json) {
  const j = typeof json === 'string' ? JSON.parse(json) : json;
  const errs = validateJourney(j);
  if (errs.length) throw new Error(`invalid journey config:\n  - ${errs.join('\n  - ')}`);
  return j;
}

export function validateJourney(j) {
  const errs = [];
  if (!Array.isArray(j?.steps) || !j.steps.length) return ['steps must be a non-empty array'];
  const ids = new Set();
  j.steps.forEach((s, i) => {
    if (!s.id) errs.push(`step ${i} has no id`);
    if (ids.has(s.id)) errs.push(`duplicate step id "${s.id}"`);
    ids.add(s.id);
    if (!['dm', 'notify'].includes(s.action)) errs.push(`step "${s.id}": unknown action "${s.action}"`);
    const from = s.after?.from;
    if (typeof s.after?.days !== 'number' || s.after.days < 0) errs.push(`step "${s.id}": after.days must be a number >= 0`);
    // A step may only wait on `entry` or on a step ALREADY defined above it —
    // that is what makes the sequence a line and not a cycle.
    if (from !== 'entry' && !ids.has(from)) errs.push(`step "${s.id}": after.from "${from}" is not entry or an earlier step`);
  });
  return errs;
}

/** The clock a lead's first step counts from: acceptance, or entry for a direct lead. */
export function entryAt(lead) {
  if (lead.journey?.entryAt) return Date.parse(lead.journey.entryAt);
  const path = journeyPath(lead);
  const stamp = path === 'invited'
    ? (lead.acceptedAt || lead.journey?.acceptedAt)
    : (lead.journey?.enteredAt);
  const t = Date.parse(stamp || '');
  return Number.isFinite(t) ? t : NaN;
}

/** invited = came through a connection request. direct = 1st-degree / Open Profile. */
export function journeyPath(lead) {
  if (lead.journey?.path) return lead.journey.path;
  if (lead.sentAt || lead.status === 'accepted' || lead.status === 'sent') return 'invited';
  return 'direct';
}

/** Terminal? Returns the reason string, or null if the lead is still walking. */
export function stopReason(lead, { suppressed = false } = {}) {
  if (lead.repliedAt) return 'replied';
  if (suppressed) return 'suppressed';
  if (lead.dmStatus === 'not-messageable') return 'notMessageable';
  if (lead.journey?.status === 'done') return 'done';
  if (lead.journey?.status === 'stopped') return lead.journey.stoppedReason || 'stopped';
  return null;
}

function stepIndex(journey, id) {
  return journey.steps.findIndex((s) => s.id === id);
}

/** The next step this lead has NOT completed, or null when the journey is finished. */
export function nextStep(lead, journey) {
  const history = lead.journey?.history || {};
  return journey.steps.find((s) => !history[s.id]) || null;
}

/**
 * When is `step` due for `lead`, in ms since epoch? NaN when the clock it waits
 * on has not been stamped yet (which means: not due, and not an error).
 */
export function dueAt(lead, journey, step) {
  const from = step.after.from;
  let base;
  if (from === 'entry') {
    base = entryAt(lead);
    // A direct lead has no acceptance to sit behind; its first wait is its own.
    if (journeyPath(lead) === 'direct' && typeof journey.direct?.waitDays === 'number') {
      return base + journey.direct.waitDays * DAY_MS;
    }
  } else {
    base = Date.parse(lead.journey?.history?.[from] || '');
  }
  if (!Number.isFinite(base)) return NaN;
  return base + step.after.days * DAY_MS;
}

/**
 * THE WHOLE DECISION for one lead. Returns
 *   { due: true,  step, dueAt }                     — act now
 *   { due: false, reason, step?, dueAt? }           — and why not
 */
export function evaluate(lead, journey, { now = Date.now(), suppressed = false } = {}) {
  const stopped = stopReason(lead, { suppressed });
  if (stopped) return { due: false, reason: stopped };

  const step = nextStep(lead, journey);
  if (!step) return { due: false, reason: 'journey complete' };

  // A stopIf is re-checked at every step, not only at entry: a lead can reply
  // between the outreach and the video, and that reply must cancel the video.
  for (const cond of step.stopIf || []) {
    if (cond === 'replied' && lead.repliedAt) return { due: false, reason: 'replied' };
  }

  const at = dueAt(lead, journey, step);
  if (!Number.isFinite(at)) return { due: false, reason: `waiting on "${step.after.from}" (not stamped yet)`, step };
  if (now < at) return { due: false, reason: 'not due yet', step, dueAt: new Date(at).toISOString() };
  return { due: true, step, dueAt: new Date(at).toISOString() };
}

/** Every lead with something due now, newest clock last so the oldest wait is served first. */
export function dueNow(leads, journey, { now = Date.now(), isSuppressed = () => false } = {}) {
  const out = [];
  for (const lead of leads) {
    const verdict = evaluate(lead, journey, { now, suppressed: isSuppressed(lead) });
    if (verdict.due) out.push({ lead, step: verdict.step, dueAt: verdict.dueAt });
  }
  return out.sort((a, b) => Date.parse(a.dueAt) - Date.parse(b.dueAt));
}

/**
 * Stamp a step as done. Called ONLY after the action actually succeeded — the
 * tick queues, the sender sends, and the stamp lands on confirmation. Stamping
 * on queue would silently skip a step that never went out.
 */
export function completeStep(lead, journey, stepId, at = new Date()) {
  lead.journey = lead.journey || {};
  lead.journey.path = lead.journey.path || journeyPath(lead);
  lead.journey.history = lead.journey.history || {};
  lead.journey.history[stepId] = at.toISOString();
  lead.journey.status = nextStep(lead, journey) ? 'active' : 'done';
  lead.journey.updatedAt = at.toISOString();
  return lead;
}

/** Enter a lead into the journey. Idempotent: a second call never resets the clock. */
export function enterJourney(lead, { path = null, at = new Date() } = {}) {
  lead.journey = lead.journey || {};
  if (lead.journey.entryAt) return lead;
  const p = path || journeyPath(lead);
  lead.journey.path = p;
  lead.journey.history = lead.journey.history || {};
  lead.journey.status = 'active';
  // An invited lead's clock is their acceptance, whenever that was — entering
  // them today must not push a follow-up that is already overdue into the future.
  const clock = p === 'invited' ? (lead.acceptedAt || at.toISOString()) : at.toISOString();
  lead.journey.entryAt = clock;
  if (p === 'direct') lead.journey.enteredAt = clock;
  lead.journey.updatedAt = at.toISOString();
  return lead;
}

/** A reply ends the journey wherever it stands. The only good outcome. */
export function markReplied(lead, at = new Date()) {
  lead.repliedAt = at.toISOString();
  lead.journey = lead.journey || {};
  lead.journey.status = 'stopped';
  lead.journey.stoppedReason = 'replied';
  lead.journey.updatedAt = at.toISOString();
  return lead;
}

export { DAY_MS, stepIndex };

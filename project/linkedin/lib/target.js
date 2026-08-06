/*
 * "AM I CLICKING THE RIGHT PERSON?"
 *
 * A LinkedIn profile page is not just one person. The rail below the top card
 * is full of OTHER members — "People you may know", "More profiles for you" —
 * and each one carries its own `Invite <Name> to connect` and `Message` button
 * inside <main>. Measured live on 2026-08-03: one profile matched SEVEN
 * `Invite ... to connect` buttons, and not one of them belonged to the person
 * whose page it was. `.first()` returned a total stranger.
 *
 * The old locator took `.first()`. So on any profile whose top card offers
 * "Follow" rather than "Connect" — extremely common — the sender would have
 * invited someone from the sidebar, watched the button flip to Pending,
 * recorded that as success, and marked the INTENDED lead as `sent`. Two people
 * burned per click: a stranger who got a cold invite, and a real lead who was
 * never contacted and is now permanently blocked from retry.
 *
 * Structure cannot be trusted to prevent that — LinkedIn reskins, and on the
 * page probed above `main h1` did not even resolve. The label can: LinkedIn
 * writes the member's own name into it. So the rule is simply that the control
 * must NAME the person we meant to reach, and if none does we stop rather than
 * click something else. That invariant survives any reskin.
 */

/**
 * Casefold, strip accents and punctuation -> comparable name tokens.
 *
 * Apostrophes are DELETED rather than turned into spaces, so "O'Brien" and
 * "OBrien" normalise the same. Turning them into spaces made those two
 * different token sets, and the two spellings genuinely occur: the company walk
 * copies LinkedIn's own rendering while li-finder gets its name off a web page.
 * A mismatch is not dangerous — we skip rather than misfire — but it silently
 * costs a qualified lead, which is the failure mode this channel can least
 * afford. Hyphens DO become spaces ("Jean-Pierre" ~ "Jean Pierre").
 */
export function normalizeName(s) {
  return String(s ?? '')
    .normalize('NFD').replace(/[̀-ͯ]/g, '')   // é -> e
    .toLowerCase()
    .replace(/['‘’ʼ]/g, '')              // O'Brien -> obrien
    .replace(/[^a-z0-9\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * Does `ariaLabel` ("Invite Ahmed Nasser to connect") refer to `name`?
 *
 * Deliberately strict-ish: every token of the expected name must appear in the
 * label. A missing middle name or a suffix on LinkedIn's side is tolerated;
 * a different person is not. Returns false for an empty expectation, because
 * "no name" must never authorise a click.
 */
export function labelNamesPerson(ariaLabel, name) {
  const want = normalizeName(name);
  const got = normalizeName(ariaLabel);
  if (!want || !got) return false;
  const wantTokens = want.split(' ').filter((t) => t.length > 1);
  if (wantTokens.length === 0) return false;
  const gotTokens = new Set(got.split(' '));
  return wantTokens.every((t) => gotTokens.has(t));
}

/**
 * First control in `locator` whose aria-label names `name`, or null.
 * Never falls back to an index — "none matched" is a valid, safe answer.
 */
export async function findControlFor(locator, name, { max = 25 } = {}) {
  const n = Math.min(await locator.count().catch(() => 0), max);
  for (let i = 0; i < n; i++) {
    const el = locator.nth(i);
    const label = await el.getAttribute('aria-label').catch(() => null);
    if (labelNamesPerson(label, name)) return { locator: el, label };
  }
  return null;
}

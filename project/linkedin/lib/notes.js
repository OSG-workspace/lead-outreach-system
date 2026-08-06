export const MAX_NOTE_CHARS = 280;

const norm = (s) => s.toLowerCase().replace(/[^a-z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
const words = (s) => norm(s).split(' ').filter(Boolean);

export function shingles(text, n = 3) {
  const w = words(text);
  const out = new Set();
  for (let i = 0; i + n <= w.length; i++) out.add(w.slice(i, i + n).join(' '));
  if (out.size === 0 && w.length) out.add(w.join(' '));
  return out;
}

export function jaccard(a, b) {
  const A = shingles(a); const B = shingles(b);
  if (!A.size || !B.size) return 0;
  let inter = 0;
  for (const s of A) if (B.has(s)) inter++;
  return inter / (A.size + B.size - inter);
}

/** Longest shared leading run of words between two notes. */
export function sharedOpeningWords(a, b) {
  const A = words(a); const B = words(b);
  let i = 0;
  while (i < A.length && i < B.length && A[i] === B[i]) i++;
  return i;
}

export const UNIQUENESS = { maxJaccard: 0.35, maxSharedOpeningWords: 6 };

/**
 * "Materially different": low trigram overlap AND no long shared opening clause.
 * A short shared opener ("Hi Dana, saw") is fine; anything longer is a template.
 */
export function findNearDuplicates(notes, cfg = UNIQUENESS) {
  const dupes = [];
  for (let i = 0; i < notes.length; i++) {
    for (let j = i + 1; j < notes.length; j++) {
      const sim = jaccard(notes[i], notes[j]);
      const open = sharedOpeningWords(notes[i], notes[j]);
      if (sim > cfg.maxJaccard || open > cfg.maxSharedOpeningWords) {
        dupes.push({ i, j, similarity: Number(sim.toFixed(3)), sharedOpeningWords: open });
      }
    }
  }
  return dupes;
}

export function validateNote(note) {
  const errs = [];
  if (typeof note !== 'string' || !note.trim()) errs.push('note is empty');
  else {
    if (note.length > MAX_NOTE_CHARS) errs.push(`note is ${note.length} chars (max ${MAX_NOTE_CHARS})`);
    if (/\{\{|\[\[|<[A-Z_]+>/.test(note)) errs.push('note contains an unfilled placeholder');
  }
  return errs;
}

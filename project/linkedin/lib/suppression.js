import fs from 'node:fs';
import { normalizeProfileUrl } from './state.js';

/*
 * The suppression list is AUTHORITATIVE ACROSS BOTH CHANNELS. An email opt-out
 * blocks LinkedIn, and vice versa. It is a plain markdown file in the shared
 * vault so the email pipeline and this module read the exact same source.
 * Any email address or linkedin.com/in/ URL appearing anywhere in the file
 * suppresses that person — format is deliberately forgiving.
 */

const EMAIL_RE = /[\w.+-]+@[\w-]+\.[\w.-]+/g;
const LI_RE = /https?:\/\/[^\s)>\]]*linkedin\.com\/in\/[^\s)>\]]+/gi;

export function loadSuppression(files) {
  const emails = new Set();
  const profiles = new Set();
  for (const f of [].concat(files).filter(Boolean)) {
    if (!fs.existsSync(f)) continue;
    const text = fs.readFileSync(f, 'utf8');
    for (const e of text.match(EMAIL_RE) || []) emails.add(e.toLowerCase());
    for (const u of text.match(LI_RE) || []) {
      const n = normalizeProfileUrl(u);
      if (n) profiles.add(n);
    }
  }
  return {
    emails,
    profiles,
    blocks(lead) {
      const url = normalizeProfileUrl(lead.linkedinUrl || lead.profileUrl);
      if (url && profiles.has(url)) return 'linkedin-url suppressed';
      const email = (lead.email || '').trim().toLowerCase();
      if (email && emails.has(email)) return 'email suppressed';
      return null;
    },
  };
}

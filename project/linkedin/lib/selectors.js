/*
 * ONE place for every selector. If one of these stops matching, the sender STOPS.
 * It does not guess an alternative and it does not fall back to text search —
 * a wrong guess on LinkedIn means clicking something you did not intend.
 * Fix the selector here, re-run T12 (selector probe), then resume.
 */
export const SELECTORS = {
  // Top-card "Connect" on a profile. LinkedIn labels it "Invite <Name> to connect".
  connectButton: 'main button[aria-label^="Invite"][aria-label*="to connect"]',
  // "More" overflow, when Connect is not surfaced directly on the top card.
  moreButton: 'main button[aria-label^="More actions"]',
  moreConnectItem: 'div.artdeco-dropdown__content--is-open div[aria-label^="Invite"][aria-label*="to connect"]',
  // Invite modal
  inviteModal: 'div[role="dialog"] .send-invite, div[role="dialog"][aria-labelledby*="send-invite"]',
  addNoteButton: 'div[role="dialog"] button[aria-label="Add a note"]',
  noteTextarea: 'div[role="dialog"] textarea#custom-message',
  sendInviteButton: 'div[role="dialog"] button[aria-label="Send invitation"]',
  // Post-send confirmation: the top-card control flips to Pending.
  // ALSO the handle scripts/withdraw_pending.js clicks to retire a stale
  // invite — and, like every per-person control, it is resolved by NAME
  // (lib/target.js), never by position: the recommendations rail carries
  // Pending buttons for other members too.
  pendingButton: 'main button[aria-label^="Pending"]',
  // The confirmation LinkedIn asks for after clicking Pending.
  withdrawConfirmButton:
    'div[role="dialog"] button[aria-label="Withdraw"], '
    + 'div[role="dialog"] button:has-text("Withdraw")',

  // --- Direct message (messenger.js) ---------------------------------------
  // Top-card "Message" on a profile. Present for 1st-degree connections and for
  // Open Profile members; absent otherwise, which is exactly how we detect that
  // a person is not messageable without an InMail credit. If it is missing the
  // sender STOPS for that person rather than falling back to a connect.
  messageButton: 'main button[aria-label^="Message"]',
  // The compose overlay that opens on the profile.
  messageOverlay: '.msg-form, div.msg-overlay-conversation-bubble',
  messageBox: '.msg-form__contenteditable[contenteditable="true"]',
  messageSendButton: '.msg-form__send-button:not([disabled])',
  // An InMail compose differs: it carries a subject field and spends a credit.
  // We detect it so we can REFUSE to spend credits unless explicitly allowed.
  inmailSubject: 'input[name="subject"], .msg-form__subject',
  // --- Company walk + profile harvest (walk_companies.js) ------------------
  // The walk feeds the person gates in tools/scripts/qualify_people.py. Every
  // field those gates test has to be READ from a page here — a gate with no
  // harvester silently fails every person, which is exactly how this arm
  // produced zero for its entire existence.
  companySearchResult: 'a[href*="/company/"]',
  companyPeopleCard: 'li.org-people-profile-card__profile-card-spacing',
  companyProfileLink: 'a[href*="/in/"]',
  // Company headcount, e.g. "11-50 employees". Feeds small_firm_headcount.
  companySizeLine: '.org-top-card-summary-info-list__info-item',
  companyLocationLine: '.org-top-card-summary-info-list__info-item',
  // Profile top card: degree ("1st"/"2nd"/"3rd") drives reachability.
  profileDegree: 'main .distance-badge .dist-value, main span.dist-value',
  // Open Profile members can be messaged with no connection at all.
  profileOpenBadge: 'main button[aria-label^="Message"]',
  // "N mutual connections" on the top card.
  profileMutuals: 'main a[href*="facetNetwork"], main span.text-body-small:has-text("mutual")',
  // Education section, for the school hook.
  profileEducation: 'section:has(div#education) li span[aria-hidden="true"]',
  // Recent activity: post cards and their relative timestamps ("2w", "3mo").
  profileActivityItem: 'main div.feed-shared-update-v2, main li.profile-creator-shared-feed-update__container',
  profileActivityAge: 'span.update-components-actor__sub-description, span.feed-shared-actor__sub-description',

  // --- Acceptance sweep (scripts/sweep_acceptance.js) ----------------------
  // The SENT-invitations manager. Anything still listed here is still pending;
  // an invite we sent that has left this list was either accepted or it
  // expired, and only a profile visit can tell those apart.
  sentInvitationCard: 'li.invitation-card, div.invitation-card',
  sentInvitationLink: 'a[href*="/in/"]',
  sentInvitationsNext: 'button[aria-label="Next"]',
  // LinkedIn's own "nothing here" state for the sent list. It is what tells a
  // genuinely empty list apart from a renamed card selector — and the two mean
  // opposite things, so the sweep refuses to guess between them. Without this,
  // the day every outstanding invite has been answered is the day the sweep
  // starts failing permanently, and no DM is ever released again.
  sentInvitationsEmpty:
    'section.artdeco-empty-state, div.artdeco-empty-state, '
    + 'div.mn-invitation-manager__empty-state, [data-test-id*="empty"]',

  // Session / challenge markers
  feedMarker: 'main',
};

export const CHALLENGE_PATTERNS = [
  /\/checkpoint\//i,
  /\/authwall/i,
  /\/uas\/login/i,
  /^https:\/\/[^/]*linkedin\.com\/login/i,
];

/*
 * Phrases that appear as VISIBLE TEXT on a genuine challenge page.
 *
 * Matched against the page with markup stripped, never against raw HTML — see
 * detectChallenge(). Two rules, learned on 2026-08-03 when a live probe of an
 * ordinary /feed/ tripped the breaker:
 *
 *   1. NO BARE WORDS. "captcha" used to be in this list, and LinkedIn embeds
 *      Google reCAPTCHA Enterprise on normal pages, so that string is in the
 *      HTML of every healthy page. The first profile the sender opened would
 *      have thrown HardStop('challenge'), written a 48h cooldown, and bricked
 *      the channel for two days on its first real action.
 *   2. The RELIABLE detectors are the URL (CHALLENGE_PATTERNS) and the HTTP
 *      status. Text is corroboration; keep it to whole phrases a human would
 *      actually read on the interstitial.
 */
export const CHALLENGE_TEXT = [
  'security verification',
  'quick security check',
  'unusual activity',
  'please solve this puzzle',
  'verify you are a human',
  'complete this security check',
];

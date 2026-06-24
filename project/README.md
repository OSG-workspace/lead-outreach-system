# Lead Outreach System — Free Stack Edition (⚠️ 2025, DEPRECATED)

> **⚠️ This README describes the older 2025 free-stack flow (Google Maps scraper, Instagram, LinkedIn, the `pipeline-orchestrator` skill, `/find-leads`, manual approval gate). The system has since pivoted to the `/fire` Haiku-DDG orchestrator. For the CURRENT system read, in order: [`ARCHITECTURE.md`](ARCHITECTURE.md) (canonical map) → [`CLAUDE.md`](CLAUDE.md) (fire router) → [`.claude/commands/fire.md`](.claude/commands/fire.md) (playbook). Do not follow the Maps/IG/LI instructions below — that path is no longer wired.**

A Claude Code project that searches leads across Google Maps, Instagram, LinkedIn, and the open web; scores them against a flexible per-run ICP; analyzes business gaps; and sends personalized outreach via Brevo.

**Zero scraper costs.** All sourcing runs through free, open-source tools — no Apify, no SerpAPI, no paid scrape APIs. Wired to your Obsidian vault for full memory across runs.

The only paid thing in the stack is Brevo for sending email, and Brevo's free tier (300 emails/day) covers most personal use.

## What it does

You type:

```
/find-leads "independent dentists in Beirut, pitch on bad websites"
```

Claude Code:

1. Reads your vault for context (voice, offer, who you've already contacted)
2. Builds a scoring rubric for this specific run
3. Sources ~60 leads from Google Maps + the web (free)
4. Scores each lead 0-100 against the rubric
5. Deep-researches the top ~30 to find their specific business gap
6. Drafts a personalized cold email referencing that exact gap (no generic AI fluff)
7. Sends via Brevo
8. Writes everything back to your Obsidian vault

By the next run, the system remembers everyone it's contacted.

## The free stack

| Channel | Tool | Status |
|---|---|---|
| Google Maps | [`gosom/google-maps-scraper`](https://github.com/gosom/google-maps-scraper) (Go, MIT, 3.3k★) | ✅ Excellent. No login. ~120 places/min. Built-in email extraction. |
| Web search | [`ddgs`](https://pypi.org/project/ddgs/) (Python, MIT) | ✅ Excellent. No key. No real limits. |
| Web scraping | [`crawl4ai`](https://github.com/unclecode/crawl4ai) (Python, Apache 2.0, most-starred crawler) | ✅ Excellent. LLM-friendly markdown. Handles JS pages. |
| Instagram | [`instaloader`](https://github.com/instaloader/instaloader) (Python, MIT) | ⚠️ Works, but anonymous is rate-limited to ~1-2 req/30s in 2026. Use a throwaway IG account for any real volume. |
| LinkedIn | [`joeyism/linkedin_scraper`](https://github.com/joeyism/linkedin_scraper) (Python + Playwright) | ⚠️ Works only with your `li_at` browser cookie. Account-flag risk — use a secondary account. |

## Honest tradeoffs vs the paid Apify version

| | Free stack | Apify |
|---|---|---|
| Google Maps | Identical quality | Identical quality |
| Web scraping | Identical quality | Identical quality |
| Instagram (anonymous) | ~30 profiles/run, slow | 200+ profiles/run, fast |
| Instagram (logged in) | 100-200/run, account flag risk | Same volume, no risk to YOUR account |
| LinkedIn | 30-50/run with cookie, account flag risk | 100+/run, no risk to your account |
| Setup time | ~10 min one-time | ~2 min |
| Cost per run | $0 | $1-3 |
| Best for | Personal use, low-medium volume | Agencies, high volume |

For 30-email cold outreach campaigns the free stack is all you need.

## Setup (one-time, ~10 minutes)

### Step 1 — Install base requirements

You need these on your system already:

- **macOS**: `brew install git python@3.11 node`
- **Linux**: install via apt/yum: `git python3 python3-venv python3-pip nodejs curl`
- Recommended (cleanest install): **Docker** (`brew install --cask docker` on Mac, or Docker Desktop)
- Alternative if no Docker: **Go 1.25+** (`brew install go`)

### Step 2 — Clone & install the scrapers

```bash
cd /path/to/your/projects
unzip lead-outreach-system-free.zip
cd lead-outreach-system-free

chmod +x ./tools/install.sh
./tools/install.sh
```

The installer detects your OS, installs Google Maps scraper (via Docker if available, else Go), creates a Python venv, and installs `instaloader`, `linkedin_scraper`, `ddgs`, `crawl4ai` + Playwright Chromium.

Takes ~5 minutes on a fresh machine.

### Step 3 — Get the API keys you DO need

- **Brevo MCP token** — Brevo dashboard > SMTP & API > API Keys & MCP > generate, toggle "Create MCP server API key"
- **Verify your sender email in Brevo** — Settings > Senders & IP. Verify your full domain (DKIM + SPF) for better deliverability.

### Step 4 — Configure `.env`

```bash
cp .env.example .env
# Edit .env and fill in:
#   BREVO_MCP_TOKEN
#   BREVO_SENDER_EMAIL
#   BREVO_SENDER_NAME
#   OBSIDIAN_VAULT_PATH  (absolute path to your vault root)
```

### Step 5 — (Optional, only if scraping IG/LI)

#### For Instagram

Create a throwaway Instagram account (NOT your personal one). Add to `.env`:

```
IG_USERNAME=your_throwaway_handle
IG_PASSWORD=your_throwaway_password
```

If 2FA is enabled, run this once interactively to clear the challenge:

```bash
source ./tools/venv/bin/activate
instaloader --login=$IG_USERNAME
deactivate
```

#### For LinkedIn

Extract your `li_at` cookie from a logged-in browser:

1. Open Chrome, log in to LinkedIn
2. DevTools (Cmd+Opt+I) → Application → Cookies → linkedin.com
3. Find `li_at`, copy the value
4. Paste into `.env` as `LI_AT_COOKIE=<value>`

Use a **secondary** LinkedIn account, not your main one. The cookie expires every ~12 months.

### Step 6 — Verify everything works

```bash
./tools/scripts/test-maps.sh                              # Always run
./tools/run.sh tools/scripts/test-ddg.py                  # Always run
./tools/run.sh tools/scripts/test-crawl4ai.py             # Always run
./tools/run.sh tools/scripts/test-instagram.py            # only if IG creds set
./tools/run.sh tools/scripts/test-linkedin.py             # only if LI cookie set
```

All required tests must pass before continuing.

### Step 7 — Open in Claude Code

```bash
claude
```

The `.mcp.json` auto-loads Brevo + MCPVault. First time you'll be prompted to authorize each — accept.

### Step 8 — Bootstrap the vault

```
/vault-bootstrap
```

Creates `lead-outreach/` in your Obsidian vault with templates. Then **fill in three files in Obsidian**:

- `lead-outreach/voice.md` — how your emails sound
- `lead-outreach/offer.md` — what you actually deliver
- `lead-outreach/compliance.md` — physical address (legally required for cold email)

### Step 9 — Run a campaign

```
/find-leads "your target description, pitch on the gap"
```

Examples:

```
/find-leads "independent dentists in Beirut, pitch on outdated websites"
/find-leads "Shopify pet stores under 10k followers, pitch on weak email marketing"
/find-leads "yoga instructors on Instagram in Dubai 1k-10k followers, pitch on no booking flow"
/find-leads "B2B SaaS founders on LinkedIn series A, pitch on weak content marketing"
```

## File structure

```
lead-outreach-system/
├── CLAUDE.md                       # Master context loaded every session
├── .mcp.json                       # MCP config (Brevo + MCPVault only)
├── .env.example                    # Template — copy to .env
├── .gitignore
├── README.md
├── tools/                          # Free scrapers (created by install.sh)
│   ├── install.sh                  # One-time installer
│   ├── google-maps-scraper         # gosom binary or Docker wrapper
│   ├── venv/                       # Python venv (instaloader, ddgs, etc.)
│   ├── run.sh                      # Wrapper to run Python tools in venv
│   └── scripts/                    # Helpers + sanity checks
│       ├── filter_maps_leads.py    # Trim raw scraper output → canonical schema (token saver)
│       ├── extract_signals.py      # crawl4ai → ~1KB signals per page (token saver)
│       ├── batch_leads.py          # Split leads into batches for chunked scoring
│       ├── test-maps.sh
│       ├── test-ddg.py
│       └── test-crawl4ai.py
├── .claude/
│   ├── skills/                     # 12 progressive-disclosure skills
│   │   ├── setup-tools/                # Installs the free toolchain
│   │   ├── pipeline-orchestrator/      # Runs the full flow
│   │   ├── icp-definition/             # Per-run scoring rubric
│   │   ├── lead-sourcing-maps/         # → ./tools/google-maps-scraper
│   │   ├── lead-sourcing-instagram/    # → instaloader
│   │   ├── lead-sourcing-linkedin/     # → linkedin_scraper or DDG fallback
│   │   ├── lead-sourcing-web/          # → ddgs + crawl4ai
│   │   ├── lead-scoring/               # 0-100 with breakdown
│   │   ├── business-gap-analysis/      # Deep research per lead
│   │   ├── outreach-copywriting/       # No-fluff personalized emails
│   │   ├── brevo-send/                 # Brevo MCP send
│   │   └── obsidian-memory/            # Vault read/write
│   └── commands/                   # Slash commands
│       ├── setup-tools.md          # /setup-tools
│       ├── vault-bootstrap.md      # /vault-bootstrap
│       ├── find-leads.md           # /find-leads
│       └── analyze-lead.md         # /analyze-lead
└── runs/                           # Per-campaign artifacts (gitignored)
```

## Troubleshooting

**`./tools/install.sh: command not found` or permission denied**
```bash
chmod +x ./tools/install.sh && ./tools/install.sh
```

**Google Maps scraper times out**
The default `-exit-on-inactivity 3m` waits 3 min for new results before exiting. If your queries are very narrow it might exit early with few results. Try broader queries.

**Instagram says "Please wait a few minutes before you try again"**
Anonymous mode hit a rate limit. Add `IG_USERNAME` and `IG_PASSWORD` to `.env`, or wait 30+ minutes.

**LinkedIn cookie test fails with login wall**
Cookie expired or LinkedIn flagged your IP. Extract a fresh cookie. If it still fails, the account may be soft-restricted — wait 24h or use a different account.

**`crawl4ai` fails on first run with Playwright error**
Run `./tools/venv/bin/playwright install chromium` manually. On Linux you may need additional system libs (the install.sh prints which).

**Brevo says "sender not verified"**
Go to https://app.brevo.com/senders/list, verify the email in `BREVO_SENDER_EMAIL`. For real campaigns, verify the full domain with DKIM + SPF.

**Emails landing in spam**
Cold email deliverability is its own discipline:
- Verify your domain in Brevo (DKIM + SPF + DMARC)
- Warm up the sending domain for 2-4 weeks before high volume
- Keep daily volume under 30 from a single email address
- Never use linkable images in step 1 of a sequence
- The compliance footer in `compliance.md` is legally required (CAN-SPAM)

## When to upgrade to Apify

Free stack hits practical limits at:

- **>200 IG profiles per run** → IG flags throwaway accounts, you need rotating proxies
- **>100 LinkedIn profiles per day** → LinkedIn flags personal cookies, you need professional infrastructure
- **>2,000 Maps leads per run** → free stack handles it but takes hours

If you hit these, swap the relevant `lead-sourcing-*` skill back to Apify-based (the Apify version of this system is a 5-minute migration — same skill names, same lead schema, different bash commands inside the skill).

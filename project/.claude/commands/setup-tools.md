---
description: One-time installer for the free scraping stack (Google Maps scraper, ddgs, crawl4ai). Run this first, before any campaign.
---

# /setup-tools

Install all the free scraping tools this system uses. Idempotent — safe to run multiple times.

## How to use

```
/setup-tools
```

## What this does

Runs `./tools/install.sh` which installs:

- **gosom/google-maps-scraper** — Go binary for Google Maps (Docker or source build)
- **ddgs** — Python library for DuckDuckGo search
- **crawl4ai** + Playwright Chromium — Python web scraping with markdown output

Plus test scripts at `./tools/scripts/` so you can verify each tool works.

Instagram and LinkedIn channels were removed from this stack — do not re-add `instaloader`, `linkedin_scraper`, `IG_USERNAME`, `IG_PASSWORD`, or `LI_AT_COOKIE`.

## Instructions to Claude

If `tools/install.sh` doesn't exist yet, this is the very first run — the user just cloned the repo. Walk through the `setup-tools` skill's procedure step by step.

If `tools/install.sh` exists, just run it:

```bash
chmod +x ./tools/install.sh
./tools/install.sh
```

Then guide the user through the remaining manual setup:

- **Required**: copy `.env.example` to `.env` and fill in `BREVO_MCP_TOKEN`, `BREVO_SENDER_EMAIL`, `OBSIDIAN_VAULT_PATH`

After everything is set, run the test scripts:

```bash
./tools/scripts/test-maps.sh
./tools/run.sh tools/scripts/test-ddg.py
./tools/run.sh tools/scripts/test-crawl4ai.py
```

Report a checklist of what passed/failed at the end. Don't proceed to `/find-leads` if any required tool failed.

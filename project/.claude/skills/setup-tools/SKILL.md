---
name: setup-tools
description: One-time installer for the free scraping tools used by this stack (gosom/google-maps-scraper, ddgs, crawl4ai). Use on first run, when /find-leads complains a tool is missing, or when the user explicitly runs /setup-tools. Idempotent — safe to re-run. Instagram and LinkedIn channels have been removed; do not re-add them.
---

# Setup Tools (Free Stack Installer)

Installs every free scraper this system uses. Detects what's missing and installs only that. Idempotent.

## Pre-flight checks

Before installing anything, verify the host environment:

```bash
# 1. macOS / Linux only — Windows users need WSL
uname -s

# 2. Required base tools
command -v git || echo "MISSING: git"
command -v python3 || echo "MISSING: python3"
command -v go || echo "MISSING: go (will install via brew if on macOS)"
command -v node || echo "MISSING: node (needed for MCP servers)"

# 3. Python version
python3 --version  # need 3.10+
```

If anything is missing, tell the user exactly what to install (with the right commands for their OS) before continuing. Don't try to install system-level dependencies for them.

For macOS, recommend Homebrew:
```bash
brew install git python@3.11 go node
```

## Step 1 — Google Maps scraper (gosom)

This is a Go binary. Install method depends on what's available:

### Option A — Pre-built binary (faster, recommended)

```bash
mkdir -p tools
cd tools
# Detect OS and arch
OS=$(uname -s | tr '[:upper:]' '[:lower:]')   # darwin / linux
ARCH=$(uname -m)                              # x86_64 / arm64

# Map to release naming
case "$ARCH" in
  x86_64) ARCH="amd64" ;;
  arm64|aarch64) ARCH="arm64" ;;
esac

# Pull latest release URL via GitHub API
LATEST_URL=$(curl -s https://api.github.com/repos/gosom/google-maps-scraper/releases/latest \
  | grep "browser_download_url.*${OS}.*${ARCH}" \
  | head -1 \
  | cut -d '"' -f 4)

if [ -z "$LATEST_URL" ]; then
  echo "Pre-built binary not found for $OS/$ARCH — falling back to source build"
else
  curl -L -o gmaps-scraper.tar.gz "$LATEST_URL"
  tar -xzf gmaps-scraper.tar.gz
  chmod +x google-maps-scraper
  rm gmaps-scraper.tar.gz
  cd ..
fi
```

### Option B — Build from source (fallback)

```bash
cd tools
git clone https://github.com/gosom/google-maps-scraper.git gosom-src
cd gosom-src
go mod download
go build -o ../google-maps-scraper
cd ..
rm -rf gosom-src
cd ..
```

### Option C — Docker (cleanest, most reliable)

If Docker is installed, prefer this — no Go toolchain needed, no Playwright deps:

```bash
docker pull gosom/google-maps-scraper:latest
# Create a wrapper script at tools/google-maps-scraper
cat > tools/google-maps-scraper << 'EOF'
#!/bin/bash
docker run --rm \
  -v "$(pwd):/workdir" \
  -w /workdir \
  gosom/google-maps-scraper:latest \
  "$@"
EOF
chmod +x tools/google-maps-scraper
```

After install, verify:

```bash
./tools/google-maps-scraper -h | head -20
```

Should print the help text. If yes, success — set `GMAPS_SCRAPER_PATH=./tools/google-maps-scraper` in `.env`.

### Playwright dependency note

The Playwright variant (default) downloads Chromium browser libs on first run (~300MB). On Linux, may also need:

```bash
# Debian/Ubuntu
sudo apt-get install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2
```

If on macOS/Windows, this just works. If on Linux and the libs install fails, switch to the Rod variant: `docker pull gosom/google-maps-scraper:latest-rod` (uses chromedp instead of Playwright, fewer deps).

## Step 2 — Python scrapers

Use a project-local venv to avoid polluting the user's system Python:

```bash
python3 -m venv tools/venv
source tools/venv/bin/activate

pip install --upgrade pip

# Core scrapers
pip install ddgs                      # DuckDuckGo search (note: NOT duckduckgo-search, that's the old name)
pip install crawl4ai                  # Web scraping w/ Playwright
pip install playwright
playwright install chromium           # Browser binary for crawl4ai

deactivate
```

Write a `tools/run.sh` wrapper so other skills can invoke Python tools without dealing with venv activation:

```bash
cat > tools/run.sh << 'EOF'
#!/bin/bash
# Usage: ./tools/run.sh script.py [args...]
set -e
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$PROJECT_ROOT/tools/venv/bin/activate"
python3 "$@"
EOF
chmod +x tools/run.sh
```

## Step 3 — Brevo sender verification check

Brevo will reject sends from unverified senders. Check that the sender in `.env` is verified:

```bash
# This requires the Brevo MCP to be loaded. If it's not, skip with a warning.
# Otherwise, call brevo:get-senders and grep for BREVO_SENDER_EMAIL.
```

If unverified, tell the user:

```
Your sender (BREVO_SENDER_EMAIL) is not yet verified in Brevo.
Go to: https://app.brevo.com/senders/list
Click "Add a sender", verify the email.
For better deliverability, also verify your full domain (DKIM + SPF).
```

## Step 6 — Test scripts

Create a few sanity-check scripts in `tools/scripts/` so the user can verify things work:

### `tools/scripts/test-maps.sh`

```bash
#!/bin/bash
echo "Testing Google Maps scraper..."
echo "coffee shop New York" > /tmp/test-query.txt
./tools/google-maps-scraper -input /tmp/test-query.txt -results /tmp/test-maps.json -json -depth 1 -c 2 -exit-on-inactivity 30s
COUNT=$(wc -l < /tmp/test-maps.json)
echo "Found $COUNT places. Sample:"
head -1 /tmp/test-maps.json | python3 -m json.tool | head -20
```

### `tools/scripts/test-ddg.py`

```python
from ddgs import DDGS
with DDGS() as ddgs:
    results = list(ddgs.text("dental clinics new york", max_results=5))
print(f"Found {len(results)} results")
for r in results[:3]:
    print(f"- {r['title']}\n  {r['href']}")
```

### `tools/scripts/test-crawl4ai.py`

```python
import asyncio
from crawl4ai import AsyncWebCrawler

async def main():
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun("https://example.com")
        print("Success:", result.success)
        print("Markdown length:", len(result.markdown))
        print(result.markdown[:200])

asyncio.run(main())
```

## Step 5 — Wrap up

After all installs succeed, print a checklist:

```
✓ Google Maps scraper      → ./tools/google-maps-scraper
✓ Python venv              → ./tools/venv (ddgs, crawl4ai)
✓ Run wrapper              → ./tools/run.sh
✓ Test scripts             → ./tools/scripts/

Required:
☐ BREVO_MCP_TOKEN set in .env
☐ BREVO_SENDER_EMAIL verified in Brevo dashboard
☐ OBSIDIAN_VAULT_PATH set in .env

Run sanity checks:
  ./tools/scripts/test-maps.sh
  ./tools/run.sh tools/scripts/test-ddg.py
  ./tools/run.sh tools/scripts/test-crawl4ai.py

When all green, run /vault-bootstrap then /find-leads "your target"
```

## Failure modes

- **`go build` fails**: user lacks Go 1.25+. Either upgrade Go OR switch to Docker mode.
- **`playwright install chromium` fails**: usually a missing system lib. Print the apt/brew command for their OS.
- **`pip install crawl4ai` fails on Apple Silicon**: known issue with some deps. Tell user to upgrade pip first (`pip install --upgrade pip`) and retry. If still failing, use Python 3.11 specifically.
- **Tools install but tests fail**: probably an IP-level issue (VPN/proxy interfering). Have user disable VPN, retest.

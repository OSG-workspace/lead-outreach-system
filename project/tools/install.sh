#!/bin/bash
# Lead Outreach System — Free Stack Installer
# Run from project root: ./tools/install.sh
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "═══════════════════════════════════════════════════════════════"
echo "  Lead Outreach System — Free Stack Installer"
echo "═══════════════════════════════════════════════════════════════"

# ── Pre-flight ──────────────────────────────────────────────────────
echo ""
echo "▶ Pre-flight checks..."

OS=$(uname -s)
ARCH=$(uname -m)
echo "   OS:   $OS / $ARCH"

MISSING=()
for cmd in git python3 curl; do
  if ! command -v $cmd &> /dev/null; then
    MISSING+=("$cmd")
  fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
  echo "   ✗ Missing: ${MISSING[*]}"
  echo ""
  if [ "$OS" = "Darwin" ]; then
    echo "   On macOS install via Homebrew:"
    echo "   brew install ${MISSING[*]}"
  else
    echo "   On Linux install via your package manager (apt/yum/etc.)"
  fi
  exit 1
fi
echo "   ✓ Base tools present"

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo $PY_VERSION | cut -d. -f1)
PY_MINOR=$(echo $PY_VERSION | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || ([ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]); then
  echo "   ✗ Python 3.10+ required (found $PY_VERSION)"
  exit 1
fi
echo "   ✓ Python $PY_VERSION"

# ── Step 1: Google Maps scraper ─────────────────────────────────────
echo ""
echo "▶ Installing Google Maps scraper..."

mkdir -p tools

if [ -f "tools/google-maps-scraper" ]; then
  echo "   ✓ Already installed, skipping"
else
  # Prefer Docker if available — least friction
  if command -v docker &> /dev/null && docker ps &> /dev/null; then
    echo "   Using Docker mode..."
    docker pull gosom/google-maps-scraper:latest > /dev/null
    cat > tools/google-maps-scraper << 'EOF'
#!/bin/bash
docker run --rm \
  -v "$(pwd):/workdir" \
  -w /workdir \
  gosom/google-maps-scraper:latest \
  "$@"
EOF
    chmod +x tools/google-maps-scraper
    echo "   ✓ Installed via Docker"
  elif command -v go &> /dev/null; then
    echo "   Using Go build..."
    cd tools
    git clone --depth 1 https://github.com/gosom/google-maps-scraper.git gosom-src > /dev/null 2>&1
    cd gosom-src
    go mod download > /dev/null 2>&1
    go build -o ../google-maps-scraper > /dev/null 2>&1
    cd ..
    rm -rf gosom-src
    cd "$PROJECT_ROOT"
    echo "   ✓ Built from source"
  else
    echo "   ✗ Need Docker OR Go to install."
    echo "   On macOS:   brew install --cask docker   (or: brew install go)"
    echo "   On Linux:   install Docker (preferred) or Go from https://go.dev"
    exit 1
  fi
fi

# ── Step 2: Python venv + scrapers ──────────────────────────────────
echo ""
echo "▶ Installing Python scrapers..."

if [ ! -d "tools/venv" ]; then
  python3 -m venv tools/venv
  echo "   ✓ Created venv"
fi

# shellcheck disable=SC1091
source tools/venv/bin/activate
pip install --quiet --upgrade pip

echo "   Installing ddgs (DuckDuckGo)..."
pip install --quiet ddgs

echo "   Installing duckdb (Overture bulk POI source)..."
# Stage 2's `overture` source adapter queries the Overture Maps parquet release
# in place over S3. Without duckdb that source exits 8 ("no ground opened") and
# the fire carries on with its other sources, so this is not fatal to install —
# but every overture-sourced campaign is silently dry until it is present.
pip install --quiet duckdb

echo "   Installing crawl4ai + Playwright..."
pip install --quiet crawl4ai
playwright install chromium > /dev/null 2>&1 || {
  echo "   ⚠ playwright install chromium had warnings (likely missing system libs)"
  echo "   On Linux you may need:  sudo apt install libnss3 libxkbcommon0 libgbm1 ..."
}

deactivate
echo "   ✓ Python scrapers installed"

# ── Step 3: make the stage shell scripts executable ─────────────────
# (no venv wrapper is generated: run_fire.py resolves tools/venv/bin/python3
#  itself, and fire_campaign.sh is the one entry point)
chmod +x tools/scripts/*.sh 2>/dev/null || true

# ── Done ────────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  ✓ Installation complete!"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "Next steps:"
echo ""
echo "  1. Copy .env.example to .env and fill in:"
echo "       BREVO_MCP_TOKEN, BREVO_SENDER_EMAIL, BREVO_SENDER_ADDRESS"
echo "       (OBSIDIAN_VAULT_PATH is optional)"
echo ""
echo "  2. Trace a campaign without spending anything:"
echo "       bash tools/scripts/fire_campaign.sh <base> --plan"
echo "     then draft only:  ... <base> --dry-run   and live:  ... <base>"
echo "     (<base> is any folder under templates/; the vault bootstraps on first run)"
echo ""

#!/usr/bin/env bash
# Stage 4: JS-rendering fetch of homepage + contact/about/team pages for every
# domain in candidates-all.txt, via crawl4ai (headless Chromium).
#
# This is now a thin wrapper around fetch_html.py. The old static-curl fetch
# could not see emails on JS-rendered SPAs / Cloudflare-challenged pages, which
# was ~75% of the extract-stage leak. crawl4ai renders the DOM before scraping.
# Output contract is unchanged: raw_html/{domain}__{slug}.html.
#
# Per kill-on-fallback we do NOT fall back to curl if crawl4ai is missing —
# fetch_html.py exits non-zero and (set -e) propagates the halt.
set -euo pipefail

RUN_DIR="${1:?Usage: fetch_html.sh <run-dir>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/../venv/bin/activate"

# shellcheck disable=SC1090
[ -f "$VENV" ] && source "$VENV"

exec python "$SCRIPT_DIR/fetch_html.py" "$RUN_DIR"

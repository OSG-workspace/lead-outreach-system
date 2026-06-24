#!/bin/bash
# Sanity check for gosom/google-maps-scraper
set -e

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

if [ ! -x "./tools/google-maps-scraper" ]; then
  echo "✗ ./tools/google-maps-scraper not found. Run ./tools/install.sh first."
  exit 1
fi

echo "Testing Google Maps scraper with a tiny query..."
TMPQ=$(mktemp)
TMPR=$(mktemp).json
echo "coffee shop New York" > "$TMPQ"

./tools/google-maps-scraper \
  -input "$TMPQ" \
  -results "$TMPR" \
  -json \
  -depth 1 \
  -c 2 \
  -exit-on-inactivity 30s

COUNT=$(wc -l < "$TMPR" | tr -d ' ')
if [ "$COUNT" -lt 1 ]; then
  echo "✗ Returned 0 results. Either Google blocked you or the binary is broken."
  exit 1
fi

echo "✓ Got $COUNT results. Sample:"
head -1 "$TMPR" | python3 -m json.tool 2>/dev/null | head -15 || head -c 500 "$TMPR"
rm "$TMPQ" "$TMPR"
echo ""
echo "✓ Maps scraper works"

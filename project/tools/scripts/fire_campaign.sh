#!/bin/bash
# ONE-COMMAND FIRE. Usage:  bash tools/scripts/fire_campaign.sh <template-base> [--dry-run|--plan]
#
# Once a campaign's fixture exists under templates/<base>/, firing is THIS
# single command — no thinking, no file inspection, no bash block to rebuild:
#   1. validates the fixture is fire-ready (per draft mode)
#   2. clones it into a NEW dated runs/ folder (never reuses one)
#   3. hands off to the deterministic orchestrator run_fire.py
# Any missing config = one-line ABORT naming the exact file to create.

set -euo pipefail
cd "$(dirname "$0")/../.."    # -> project/

BASE="${1:-}"
shift || true
[ -n "$BASE" ] || { echo "ABORT: usage: fire_campaign.sh <template-base> [--dry-run|--plan]"; exit 1; }

TPL="templates/$BASE"
[ -d "$TPL" ] || { echo "ABORT: no fixture at $TPL. Existing: $(ls templates/ | grep -v README | tr '\n' ' ')"; exit 1; }

# --- fire-readiness validation (mechanical template-first enforcement) ---
[ -f "$TPL/icp.yaml" ]    || { echo "ABORT: $TPL/icp.yaml missing."; exit 1; }
[ -f "$TPL/queries.txt" ] || { echo "ABORT: $TPL/queries.txt missing."; exit 1; }

DRAFT_MODE="template"
[ -f "$TPL/draft_mode.txt" ] && DRAFT_MODE=$(head -1 "$TPL/draft_mode.txt" | tr -d ' \n')
EMAIL_ON=1
if [ -f "$TPL/channels.json" ] && ! grep -q '"email"' "$TPL/channels.json"; then EMAIL_ON=0; fi

if [ "$DRAFT_MODE" = "template" ] && [ "$EMAIL_ON" = "1" ] && [ ! -f "$TPL/pitch.json" ]; then
    echo "ABORT: $TPL/pitch.json missing — template-mode campaigns fire ONLY with"
    echo "user-approved copy (template-first contract, see templates/README.md)."
    echo "Ask the user for their email template, or propose examples and confirm."
    exit 1
fi
if [ "$DRAFT_MODE" = "custom" ] && [ ! -s "$TPL/vertical.txt" ]; then
    echo "ABORT: $TPL/vertical.txt missing/empty — custom runs need an explicit vertical."
    exit 1
fi
if [ -f "$TPL/source_agent.txt" ]; then
    SA=$(head -1 "$TPL/source_agent.txt" | tr -d ' \n')
    [ -f "../.claude/agents/${SA}.md" ] || { echo "ABORT: agent def .claude/agents/${SA}.md missing."; exit 1; }
fi
[ -f "vault/lead-outreach/sent-log.md" ] || { echo "ABORT: vault not initialized (sent-log.md missing)."; exit 1; }

# --- always a completely NEW dated run folder ---
SLUG="$(date +%Y-%m-%d)-$BASE"; COUNTER=1
while [ -d "runs/$SLUG" ]; do
    SLUG="$(date +%Y-%m-%d)-$BASE-$COUNTER"; COUNTER=$((COUNTER+1))
done
mkdir -p "runs/$SLUG"
cp "$TPL"/* "runs/$SLUG"/ 2>/dev/null || true
echo "Bootstrapped runs/$SLUG from $TPL (draft_mode=$DRAFT_MODE)."

exec python3 tools/scripts/run_fire.py "$SLUG" "$@"

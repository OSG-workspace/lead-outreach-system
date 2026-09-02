#!/bin/bash
# ONE-COMMAND FIRE. Usage:  bash tools/scripts/fire_campaign.sh <template-base> [--dry-run|--plan] [--target-leads N]
# --target-leads N: scale the sourcing fan-out to outreach ~N leads (passed
# through to run_fire.py, which dispatches query agents in adaptive waves).
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
# A fixture is campaign.json + icp.yaml + pitch.json + places.txt [+ qualify.json].
# campaign_config.py holds the ONE schema (every knob, its default, its
# materialised file) and every check that used to live here as bash: sourcing
# shape in step with run_fire.py resolve_sourcing(), WHERE (places.txt /
# countries), the template-first pitch.json gate, the LinkedIn person gates,
# custom-mode vertical. It prints one ABORT line per problem naming the fix.
# A fixture that still has the legacy per-file layout (no campaign.json) is
# read the same way — load() builds the same config from those files.
CFG="tools/scripts/campaign_config.py"
PYV="tools/venv/bin/python3"; [ -x "$PYV" ] || PYV="python3"   # stdlib-only, either works
"$PYV" "$CFG" --fixture "$TPL" --validate || exit 1
DRAFT_MODE=$("$PYV" -c "
import sys; sys.path.insert(0, 'tools/scripts')
import campaign_config as cc; print(cc.load('$TPL')['draft_mode'])")
# The vault is per-operator and never in the repo, so a fresh clone has none.
# Create it from the tracked skeleton rather than aborting — idempotent, and it
# never overwrites an existing file, so a live vault is untouched.
bash tools/scripts/bootstrap_vault.sh
[ -f "vault/lead-outreach/sent-log.md" ] || { echo "ABORT: vault bootstrap failed (sent-log.md still missing)."; exit 1; }

# --- always a completely NEW dated run folder ---
SLUG="$(date +%Y-%m-%d)-$BASE"; COUNTER=1
while [ -d "runs/$SLUG" ]; do
    SLUG="$(date +%Y-%m-%d)-$BASE-$COUNTER"; COUNTER=$((COUNTER+1))
done
mkdir -p "runs/$SLUG"
# Materialise campaign.json into the per-file layout every stage reads
# (sourcing.json, channels.json, draft_mode.txt, …) and copy the content files
# (icp.yaml, pitch.json, places.txt, qualify.json) plus campaign.json itself,
# so the run folder stays self-documenting and no stage script changes.
"$PYV" "$CFG" --fixture "$TPL" --materialize "runs/$SLUG" || { rm -rf "runs/$SLUG"; exit 1; }
echo "Bootstrapped runs/$SLUG from $TPL (draft_mode=$DRAFT_MODE)."

# --- keep the machine awake for the whole fire ---
# A fire is a 1-3 hour wall-clock job, and EVERY sub-agent timeout in
# agent_dispatch.py is wall-clock. Machine sleep therefore does not pause a run,
# it FAILS it: on 2026-08-17-au-trades-1 the lid shut at 21:48:33 ("Clamshell
# Sleep"), the run spent ~5 of its 6.5 hours in deep idle, and name-finders that
# were doing nothing wrong were killed at rc=-1 and tripped the Stage 5.5
# kill-on-fallback gate. Three of that day's five fires died this way.
#   -i  prevent idle system sleep
#   -m  keep the disk from idling out
#   -s  prevent system sleep (holds only on AC power)
# LIMIT, stated plainly: nothing here can stop CLAMSHELL sleep on battery.
# Closing the lid unplugged still sleeps the Mac mid-run, so we warn instead.
if command -v caffeinate >/dev/null 2>&1; then
    if ! pmset -g ps 2>/dev/null | grep -q "AC Power"; then
        echo "WARNING: firing on BATTERY. caffeinate cannot prevent clamshell sleep."
        echo "         Keep the lid OPEN or plug in, or this run will stall mid-fire."
    fi
    exec caffeinate -ims python3 tools/scripts/run_fire.py "$SLUG" "$@"
fi
exec python3 tools/scripts/run_fire.py "$SLUG" "$@"

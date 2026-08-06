#!/bin/bash
# THE UNATTENDED ENTRY POINT. What launchd calls; not meant to be typed.
#
# daily.sh assumes a human already opened the LinkedIn Chrome. Unattended there
# is no human, and preflight would exit 2 every morning — a channel that looks
# scheduled but never sends, which is the exact silent failure heartbeat.js
# exists to catch. So this opens the browser first (idempotent: start_chrome.sh
# reuses an already-listening port and returns immediately), then runs the drip.
#
# The Chrome profile holds a persisted session, so no login is involved here.
# If that session ever expires, preflight stops the run with its usual message
# and nothing is half-sent.
#
# USAGE  bash scripts/autopilot.sh          # dry run
#        bash scripts/autopilot.sh --live   # what the LaunchAgent runs
set -uo pipefail
cd "$(dirname "$0")/.."

# launchd hands us a near-empty PATH; node and curl must still resolve.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

LIVE="${1:-}"
echo "=================================================================="
echo "autopilot $(date '+%Y-%m-%d %H:%M:%S %Z')  ${LIVE:-(dry run)}"
echo "=================================================================="

# 1. Browser up. A failure here is fatal for the day and says why.
if ! bash scripts/start_chrome.sh; then
    echo "autopilot: could not open the LinkedIn Chrome — skipping today."
    exit 2
fi

# Chrome needs a moment to restore the session before the first CDP attach.
sleep 5

# 2. The whole send side.
bash scripts/daily.sh $LIVE
rc=$?
echo "autopilot: daily.sh exited $rc"

# 3. Did a scheduled weekday silently produce nothing? Say so in the log.
node scripts/heartbeat.js || echo "autopilot: HEARTBEAT FAILED — see above."
exit $rc

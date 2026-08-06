#!/usr/bin/env bash
# Install the LinkedIn daily autopilot as a launchd job, on THIS machine.
#
# The plist is shipped as a .template with a __LINKEDIN_DIR__ placeholder,
# because an absolute path baked into the repo only works on the machine it was
# written on. This renders it against your actual checkout and loads it.
#
# Usage:  bash linkedin/launchd/install.sh          (install + load)
#         bash linkedin/launchd/install.sh --print  (render to stdout, do nothing)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINKEDIN_DIR="$(cd "$HERE/.." && pwd)"
TEMPLATE="$HERE/com.osg.linkedin-daily.plist.template"
LABEL="com.osg.linkedin-daily"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"

[ -f "$TEMPLATE" ] || { echo "ABORT: missing $TEMPLATE"; exit 1; }
rendered="$(sed "s|__LINKEDIN_DIR__|$LINKEDIN_DIR|g" "$TEMPLATE")"

if [ "${1:-}" = "--print" ]; then
    printf '%s\n' "$rendered"
    exit 0
fi

if [ "$(uname)" != "Darwin" ]; then
    echo "launchd is macOS-only. On Linux, adapt linkedin/cron.txt instead:"
    echo "  LI=$LINKEDIN_DIR"
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
printf '%s\n' "$rendered" > "$DEST"
echo "wrote $DEST"

launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"
echo "loaded $LABEL — runs 09:10 on weekdays."
echo
echo "It needs the LinkedIn Chrome running (bash scripts/start_chrome.sh);"
echo "without it the job exits 2 with an actionable message and skips the day."
echo "Check:   launchctl list | grep $LABEL"
echo "Logs:    $LINKEDIN_DIR/state/daily.log"
echo "Remove:  launchctl unload $DEST && rm $DEST"

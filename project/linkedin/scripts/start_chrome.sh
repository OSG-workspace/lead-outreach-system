#!/bin/bash
# Launch the LinkedIn-only Chrome the senders attach to.
#
# THE ACCOUNT IS NOT THE PROFILE. This starts a SEPARATE Chrome profile, which
# is just an empty cookie jar. You log into your normal LinkedIn account in it
# once — same account, same connections, same everything. What you gain is that
# the debugging port exposes ONLY this window: port 9222 has no authentication,
# so anything local that talks to it can drive whatever the attached browser can
# reach. Pointed at your everyday profile that would be every session you own;
# pointed here it is LinkedIn and nothing else.
#
# Chrome binds the debug port to 127.0.0.1 and validates the Host header, so it
# is not reachable from the network or from a web page. The residual exposure is
# other processes running as you, on this machine, while the window is open.
# Hence: open it to send, quit it when done.
set -euo pipefail

PORT="${LINKEDIN_CDP_PORT:-9222}"
PROFILE="${LINKEDIN_CHROME_PROFILE:-$HOME/chrome-linkedin}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DEFAULT_PROFILE="$HOME/Library/Application Support/Google/Chrome"

[ -x "$CHROME" ] || { echo "ABORT: Chrome not found at $CHROME"; exit 1; }

# --use-default-profile: run against the everyday Chrome profile, which is
# ALREADY logged in to LinkedIn — no login step at all.
#
# The cost is real and worth stating plainly: the debug port has no
# authentication, so while this window is open any process running as you can
# drive a browser that holds every session you own — mail, bank, everything —
# not just LinkedIn. On the isolated profile the same exposure reaches LinkedIn
# and nothing else.
#
# It is an explicit flag rather than an env var so it can only ever be a
# deliberate choice, never something a script inherits by accident.
if [ "${1:-}" = "--use-default-profile" ]; then
    PROFILE="$DEFAULT_PROFILE"
    echo "WARNING: opening the debug port on your EVERYDAY Chrome profile."
    echo "         While it is open, every logged-in session in it is reachable"
    echo "         by any local process. Quit this Chrome when the run is done."
    echo
else
    # Otherwise refuse the everyday profile, even via the env var.
    case "$(cd "$(dirname "$PROFILE")" 2>/dev/null && pwd)/$(basename "$PROFILE")" in
      "$DEFAULT_PROFILE"|"$DEFAULT_PROFILE"/*)
        echo "ABORT: refusing to open a debugging port on your everyday Chrome profile."
        echo "That would expose every logged-in session you have, not just LinkedIn."
        echo "If that is genuinely what you want, say so explicitly:"
        echo "    bash scripts/start_chrome.sh --use-default-profile"
        exit 1;;
    esac
fi

if curl -s --max-time 2 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
    echo "Already listening on 127.0.0.1:$PORT — reusing it."
    exit 0
fi

# A Chrome already running on this profile would silently swallow the flag: the
# launch hands off to the existing instance and the port never opens, then we
# sit in the wait loop for 30s and report a confusing timeout. Catch it here.
#
# The everyday profile is the awkward case: that Chrome runs with no
# --user-data-dir flag at all, so it cannot be matched by profile path — ANY
# running Chrome is the blocker.
if [ "$PROFILE" = "$DEFAULT_PROFILE" ]; then
    if pgrep -x "Google Chrome" >/dev/null 2>&1; then
        echo "ABORT: Chrome is already running, and a running Chrome ignores the"
        echo "debug-port flag — the launch just hands off to the existing instance."
        echo
        echo "Quit Chrome COMPLETELY first (Cmd+Q, not just closing the windows),"
        echo "then re-run this script. Your tabs will reopen."
        exit 1
    fi
elif pgrep -f "user-data-dir=$PROFILE" >/dev/null 2>&1; then
    echo "ABORT: Chrome is already running on $PROFILE without the debug port."
    echo "Quit that window (Cmd+Q) and re-run this script."
    exit 1
fi

FIRST_RUN=0
[ -d "$PROFILE" ] || FIRST_RUN=1

echo "Starting LinkedIn Chrome  profile=$PROFILE  port=$PORT"
"$CHROME" --remote-debugging-port="$PORT" \
          --user-data-dir="$PROFILE" \
          --no-first-run --no-default-browser-check \
          "https://www.linkedin.com/feed/" >/dev/null 2>&1 &

for _ in $(seq 1 30); do
    if curl -s --max-time 1 "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
        echo "Port $PORT is open."
        if [ "$FIRST_RUN" = "1" ]; then
            echo
            echo "FIRST RUN — log in to LinkedIn in the window that just opened."
            echo "Use your normal account. The session persists in $PROFILE,"
            echo "so you will not have to do this again."
        fi
        echo
        echo "Confirm which account is attached:  node scripts/whoami.js"
        exit 0
    fi
    sleep 1
done

echo "ABORT: port $PORT never opened. Is another Chrome instance holding the profile?"
exit 1

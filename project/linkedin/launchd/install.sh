#!/usr/bin/env bash
# Install the LinkedIn daily autopilot as a launchd job, on THIS machine.
#
# The plist is shipped as a .template with placeholders, because an absolute
# path baked into the repo only works on the machine it was written on. This
# renders it against your actual checkout and loads it.
#
# Usage:  bash linkedin/launchd/install.sh          (install + load + verify)
#         bash linkedin/launchd/install.sh --print  (render to stdout, do nothing)
#         bash linkedin/launchd/install.sh --check  (verify launchd can actually
#                                                    reach this checkout; no install)
#
# ---------------------------------------------------------------------------
# macOS TCC is the thing that breaks this silently.
#
# A LaunchAgent runs with NO Full Disk Access. If the checkout lives under a
# TCC-protected folder — ~/Downloads, ~/Desktop, ~/Documents, iCloud Drive — the
# agent cannot read a single file in it, and launchd cannot even open a log file
# there. The symptom is the worst kind: `launchctl list` shows the job, the job
# "runs" every weekday, and it exits 78 (EX_CONFIG) before bash ever starts,
# writing nothing to any log. Measured on this machine 2026-08-11: four missed
# weekday runs, zero output, channel silently dead since the checkout moved.
#
# So this installer does three things a plain `cp` never did:
#   1. logs OUTSIDE the checkout (~/Library/Logs), so a failure always has a trace
#   2. when the checkout IS protected, installs a dedicated ad-hoc-signed
#      interpreter (~/bin/osg-bash) and asks you to grant Full Disk Access to
#      THAT — not to /bin/bash, which would hand FDA to every shell script the
#      machine ever runs
#   3. --check proves end-to-end whether launchd can read the checkout, so you
#      never have to wait until 09:10 to find out
# ---------------------------------------------------------------------------
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LINKEDIN_DIR="$(cd "$HERE/.." && pwd)"

# Address the system through the space-free symlink when one exists, per the
# repo-root CLAUDE.md: `tools/venv/bin/activate` and `linkedin/cron.txt` are
# already pinned to it, so a checkout that later moves only needs the symlink
# repointed instead of every consumer re-rendered. (This is about stability, not
# permissions — TCC resolves the real path either way.)
LINK="$HOME/lead-outreach-system"
if [ -L "$LINK" ] && [ -d "$LINK" ]; then
    _real_link="$(cd "$LINK" && pwd -P)"
    _real_dir="$(cd "$LINKEDIN_DIR" && pwd -P)"
    case "$_real_dir" in
        "$_real_link"/*) LINKEDIN_DIR="$LINK${_real_dir#"$_real_link"}" ;;
    esac
fi
TEMPLATE="$HERE/com.osg.linkedin-daily.plist.template"
LABEL="com.osg.linkedin-daily"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGFILE="$HOME/Library/Logs/$LABEL.log"
OSG_BASH="$HOME/bin/osg-bash"
PROBE_LABEL="com.osg.linkedin-tccprobe"

# Is the checkout somewhere a LaunchAgent is forbidden to read?
is_protected() {
    case "$(cd "$LINKEDIN_DIR" && pwd -P)" in
        "$HOME"/Downloads/*|"$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Library/Mobile\ Documents/*) return 0 ;;
        *) return 1 ;;
    esac
}

# Ad-hoc-signed private copy of bash. Distinct code identity from /bin/bash, so
# the Full Disk Access grant covers this job alone. Re-signing is what makes the
# identity distinct — a bare `cp` inherits Apple's signature.
ensure_osg_bash() {
    if [ ! -x "$OSG_BASH" ]; then
        mkdir -p "$(dirname "$OSG_BASH")"
        cp /bin/bash "$OSG_BASH"
        codesign --force --sign - "$OSG_BASH" >/dev/null 2>&1 || true
        echo "created $OSG_BASH (ad-hoc signed private interpreter)"
    fi
}

# End-to-end proof: can a real LaunchAgent read a real file in this checkout?
# Nothing is sent — the probe only reads one line of autopilot.sh.
run_check() {
    local probe_plist="$HOME/Library/LaunchAgents/$PROBE_LABEL.plist"
    local probe_log="$HOME/Library/Logs/$PROBE_LABEL.log"
    local interp="/bin/bash"
    is_protected && [ -x "$OSG_BASH" ] && interp="$OSG_BASH"

    rm -f "$probe_log"
    cat > "$probe_plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$PROBE_LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$interp</string><string>-c</string>
    <string>head -1 "$LINKEDIN_DIR/scripts/autopilot.sh" >/dev/null 2>&1 && echo PROBE_OK || echo PROBE_BLOCKED</string>
  </array>
  <key>StandardOutPath</key><string>$probe_log</string>
  <key>StandardErrorPath</key><string>$probe_log</string>
</dict></plist>
PLIST

    launchctl bootout "gui/$(id -u)/$PROBE_LABEL" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$probe_plist" >/dev/null 2>&1 || true
    launchctl kickstart -w "gui/$(id -u)/$PROBE_LABEL" >/dev/null 2>&1 || true
    sleep 2
    local result; result="$(cat "$probe_log" 2>/dev/null || true)"
    launchctl bootout "gui/$(id -u)/$PROBE_LABEL" >/dev/null 2>&1 || true
    rm -f "$probe_plist" "$probe_log"

    case "$result" in
        *PROBE_OK*)
            echo "CHECK PASS — a LaunchAgent can read this checkout. The autopilot will run."
            return 0 ;;
        *PROBE_BLOCKED*|"")
            echo "CHECK FAIL — a LaunchAgent CANNOT read $LINKEDIN_DIR."
            echo
            echo "  macOS is blocking it (TCC). Until this is fixed the job runs every"
            echo "  weekday and does nothing. Grant Full Disk Access to the interpreter:"
            echo
            echo "    System Settings > Privacy & Security > Full Disk Access > [+]"
            echo "    then Cmd-Shift-G and paste:  $interp"
            echo
            [ "$interp" = "$OSG_BASH" ] && echo "  ($interp is a private copy of bash used only by this job, so the"
            [ "$interp" = "$OSG_BASH" ] && echo "   grant does not extend to every shell script on the machine.)"
            echo "  Then re-run:  bash linkedin/launchd/install.sh --check"
            return 1 ;;
    esac
}

[ -f "$TEMPLATE" ] || { echo "ABORT: missing $TEMPLATE"; exit 1; }

if [ "${1:-}" = "--check" ]; then
    [ "$(uname)" = "Darwin" ] || { echo "--check is macOS-only."; exit 1; }
    is_protected && ensure_osg_bash
    run_check
    exit $?
fi

INTERP="/bin/bash"
if is_protected; then
    [ "${1:-}" = "--print" ] || ensure_osg_bash
    INTERP="$OSG_BASH"
fi

rendered="$(sed -e "s|__LINKEDIN_DIR__|$LINKEDIN_DIR|g" \
                -e "s|__LOGFILE__|$LOGFILE|g" \
                -e "s|__BASH__|$INTERP|g" "$TEMPLATE")"

if [ "${1:-}" = "--print" ]; then
    printf '%s\n' "$rendered"
    exit 0
fi

if [ "$(uname)" != "Darwin" ]; then
    echo "launchd is macOS-only. On Linux, adapt linkedin/cron.txt instead:"
    echo "  LI=$LINKEDIN_DIR"
    exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
printf '%s\n' "$rendered" > "$DEST"
echo "wrote $DEST"

# Keep the documented `tail -f state/daily.log` working now that launchd writes
# outside the checkout.
mkdir -p "$LINKEDIN_DIR/state"
# Keep the pre-move log, but under a name the channel's .gitignore still covers
# (`state/*.log`) — a backup that lands in `git status` is noise in every later
# session.
if [ -f "$LINKEDIN_DIR/state/daily.log" ] && [ ! -L "$LINKEDIN_DIR/state/daily.log" ]; then
    mv "$LINKEDIN_DIR/state/daily.log" "$LINKEDIN_DIR/state/daily-pre-$(date +%Y%m%d).log"
fi
ln -sfn "$LOGFILE" "$LINKEDIN_DIR/state/daily.log"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$DEST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST" 2>/dev/null || launchctl load "$DEST"
echo "loaded $LABEL — runs 09:10 on weekdays."
echo

if is_protected; then
    echo "NOTE: this checkout is under a TCC-protected folder, so the agent needs"
    echo "Full Disk Access or it will do nothing, every day, silently."
    echo
fi

run_check || true

echo
echo "It needs the LinkedIn Chrome running (bash scripts/start_chrome.sh);"
echo "without it the job exits 2 with an actionable message and skips the day."
echo "Check:   launchctl list | grep $LABEL"
echo "Verify:  bash linkedin/launchd/install.sh --check"
echo "Logs:    $LOGFILE   (also $LINKEDIN_DIR/state/daily.log)"
echo "Remove:  launchctl bootout gui/\$(id -u)/$LABEL && rm $DEST"

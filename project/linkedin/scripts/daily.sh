#!/bin/bash
# THE DAILY DRIP — the whole send side, one command.
#
# A fire queues weeks of invite supply. This is what actually spends it, and it
# is the only command you need after a fire. Safe to run more than once a day:
# every step is idempotent and the limits are enforced inside the senders, not
# here.
#
#   1. top up today's invite queue from the backlog   (ramp cap, no LLM)
#   2. send those invites                             (paced across the window)
#   3. find who accepted since last time              (releases their DM)
#   4. queue those DMs
#   5. send them
#
# Steps 3-5 are what make the invites worth sending; without the sweep the
# accepted connections are never followed up and the acceptance-floor breaker
# eventually reads 0% and halts new invites.
#
# USAGE  bash scripts/daily.sh            # DRY RUN — shows what it would do
#        bash scripts/daily.sh --live     # actually send
set -uo pipefail
cd "$(dirname "$0")/.."

LIVE=""
[ "${1:-}" = "--live" ] && LIVE="--live"
[ -n "$LIVE" ] || echo "== DRY RUN (pass --live to send) =="

# One preflight for the whole run: no point queueing if we cannot send.
node scripts/preflight.js || exit 2

step() { echo; echo "== $1"; }

step "1/6 top up today's invites from the backlog"
if [ -s state/backlog.json ]; then
    node queue/generate.js --from-backlog || echo "   (no invites queued)"
else
    echo "   backlog empty — fire a campaign first"
fi

step "2/6 send invites"
# NO flock HERE. macOS ships no flock(1) — the line that used to wrap this
# exited 127 ("no such file or directory") and, because 127 is not the 1 the
# check looked for, the failure was swallowed and NOTHING WAS EVER SENT. The
# senders carry their own PID-checked lockfile (lib paths.lock / .messenger.lock),
# which is portable and recovers from a crashed holder, so the overlap guarantee
# is kept without depending on a Linux-only binary.
node send/sender.js $LIVE
rc=$?
[ $rc -eq 3 ] && echo "   (another sender run is already active — skipped)"

step "3/6 who accepted since last time"
# --commit only when live: a dry run must not mutate state.
if [ -n "$LIVE" ]; then
    node scripts/sweep_acceptance.js --commit
else
    node scripts/sweep_acceptance.js
fi

step "4/6 queue DMs for the people who accepted"
node queue/generate-dm.js --from-state

step "5/6 send those DMs"
node send/messenger.js $LIVE
[ $? -eq 3 ] && echo "   (another messenger run is already active — skipped)"

step "6/6 retire invites nobody answered"
# NOT housekeeping. An unanswered pending pile is a mass-inviting signal to
# LinkedIn, and it drags trailingAcceptance toward zero until the 25% floor
# breaker stops new invites for a bookkeeping reason rather than a real one.
# This ran nowhere before 2026-08-03: withdrawPendingAfterDays was a config key
# that only the validator read.
if [ -n "$LIVE" ]; then
    node scripts/withdraw_pending.js --commit
else
    node scripts/withdraw_pending.js
fi

echo
echo "== done. state: $(node -e '
const s=JSON.parse(require("fs").readFileSync("state/state.json","utf8"));
const l=Object.values(s.leads||{});
const n=(x)=>l.filter((y)=>y.status===x).length;
console.log(`${n("sent")} invites pending, ${n("accepted")} accepted, ` +
            `${l.filter((y)=>y.dmStatus==="sent").length} DMed`);
' 2>/dev/null || echo 'no state yet')"

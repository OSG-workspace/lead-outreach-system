#!/bin/bash
# Push the journey digest to GitHub so the cloud routine can read it.
#
# The digest is the ONLY thing this pushes — an explicit path, never `git add
# -A` — because this runs unattended from launchd and must not sweep up whatever
# else happens to be dirty in the working tree at 09:10. It also never commits
# on a dirty index it did not create, and it never pushes to a branch other than
# the one already checked out.
#
# A failure here is not a failure of the day's sending: the send already
# happened. It exits 0 loudly rather than breaking autopilot.
set -uo pipefail
cd "$(dirname "$0")/../../.."       # repo root

DIGEST="project/linkedin/ops/journey-digest.json"
[ -f "$DIGEST" ] || { echo "publish_digest: no digest to push"; exit 0; }

if git diff --quiet -- "$DIGEST"; then
    echo "publish_digest: digest unchanged — nothing to push"; exit 0
fi

branch="$(git rev-parse --abbrev-ref HEAD)"
git add -- "$DIGEST" || { echo "publish_digest: git add failed"; exit 0; }
git commit -q -m "chore(journey): digest $(date -u '+%Y-%m-%dT%H:%MZ')" -- "$DIGEST" \
    || { echo "publish_digest: commit failed"; exit 0; }

# No credential prompt is possible unattended; a push that needs one must fail
# fast rather than hang launchd until the job is killed.
if GIT_TERMINAL_PROMPT=0 git push -q origin "$branch" 2>&1; then
    echo "publish_digest: pushed to origin/$branch"
else
    echo "publish_digest: PUSH FAILED — the cloud routine will see a stale digest"
    echo "                fix: authenticate git push (gh auth setup-git) and retry"
fi
exit 0

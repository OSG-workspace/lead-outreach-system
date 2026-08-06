#!/usr/bin/env bash
# Install the repo's git hooks into .git/hooks (git does not track that folder).
#
# Also repairs a stale core.hooksPath. That config wins over .git/hooks, so if it
# points at a path that has moved or no longer exists, git silently runs NO hooks
# at all — this repo was in exactly that state (it pointed at an old Desktop
# checkout), which is why a guard installed here would never have fired.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"

current="$(git config --get core.hooksPath || true)"
if [ -n "$current" ]; then
    case "$current" in
        "$ROOT"/*|"$ROOT") ;;                       # points inside this repo, fine
        *) echo "core.hooksPath pointed outside this repo ($current) — hooks never ran."
           git config --unset core.hooksPath
           echo "  unset; git will now use $ROOT/.git/hooks" ;;
    esac
fi

mkdir -p "$ROOT/.git/hooks"
for h in "$ROOT"/tools/git-hooks/*; do
    name="$(basename "$h")"
    [ "$name" = "install.sh" ] && continue
    cp "$h" "$ROOT/.git/hooks/$name"
    chmod +x "$ROOT/.git/hooks/$name"
    echo "installed hook: $name"
done

if [ -n "$(git config --get core.hooksPath || true)" ]; then
    echo "WARNING: core.hooksPath is still set — verify it points at $ROOT/.git/hooks"
fi
echo "Done."

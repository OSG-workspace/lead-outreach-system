#!/usr/bin/env bash
# Create the vault if it is missing, so a fresh clone can fire immediately.
#
# The vault is this system's long-term memory and is deliberately NOT in the
# repo (it holds contact PII and per-operator ledgers — see project/vault/README.md).
# That used to mean a fresh clone aborted on its very first command with
# "vault not initialized". This closes that gap: the skeleton in
# project/vault-template/ is copied into place, and the append-only ledgers are
# created empty.
#
# Idempotent and non-destructive: an existing file is NEVER overwritten, so
# running this against a live vault does nothing. Safe to call on every fire.
#
# Usage:  bash tools/scripts/bootstrap_vault.sh [--quiet]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "$HERE/../.." && pwd)"          # …/project
VAULT="$PROJECT/vault/lead-outreach"
TEMPLATE="$PROJECT/vault-template/lead-outreach"
QUIET="${1:-}"

say() { [ "$QUIET" = "--quiet" ] || echo "$@"; }

[ -d "$TEMPLATE" ] || { echo "ABORT: missing $TEMPLATE (repo is incomplete)."; exit 1; }

mkdir -p "$VAULT/leads" "$VAULT/targeting"

# 1. Skeleton docs — copied only if absent, so your own copy always wins.
copied=0
while IFS= read -r rel; do
    src="$TEMPLATE/$rel"
    dst="$VAULT/$rel"
    if [ ! -f "$dst" ]; then
        mkdir -p "$(dirname "$dst")"
        cp "$src" "$dst"
        copied=$((copied + 1))
    fi
done < <(cd "$TEMPLATE" && find . -type f -name '*.md' | sed 's|^\./||')

# 2. Append-only ledgers — the pipeline's dedup nets. Empty is the correct
#    starting state; every one of these is created by appending, never rewritten.
created=0
for ledger in sent-log.md bounce-list.md suppression.md \
              sourced-log.txt disqualified-log.txt overpass-cities-fired.txt; do
    if [ ! -f "$VAULT/$ledger" ]; then
        : > "$VAULT/$ledger"
        created=$((created + 1))
    fi
done

if [ "$copied" -gt 0 ] || [ "$created" -gt 0 ]; then
    say "Vault bootstrapped at project/vault/lead-outreach/"
    say "  $copied skeleton doc(s) copied from vault-template/, $created empty ledger(s) created."
    if [ "$copied" -gt 0 ]; then
        say ""
        say "  NEXT: the skeletons are placeholders. Before your first real fire, write"
        say "  your own offer.md and voice.md — the drafters read them verbatim, so they"
        say "  are the biggest lever on what your emails sound like. compliance.md needs"
        say "  a real postal address and a working opt-out before you send commercially."
    fi
else
    say "Vault already initialised — nothing to do."
fi

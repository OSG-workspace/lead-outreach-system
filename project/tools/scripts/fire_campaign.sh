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
[ -f "$TPL/icp.yaml" ]      || { echo "ABORT: $TPL/icp.yaml missing."; exit 1; }
[ -f "$TPL/sourcing.json" ] || { echo "ABORT: $TPL/sourcing.json missing — every fixture declares its Stage-2 TARGET AUDIENCE in this ONE file: {\"selector\":\"\\\"office\\\"=\\\"lawyer\\\"\",\"vertical\":\"law\"}."; exit 1; }

# Stage 2 is deterministic enumeration for every campaign. A fixture never picks a
# code path — it only declares WHO to look for and WHERE (places.txt). Three shapes
# are accepted, and this validator must stay in step with load_sourcing() in
# run_fire.py or a fire-ready fixture gets rejected here before it is ever cloned:
#   {selector, vertical}                    one OSM target
#   {targets: [{selector, vertical}, …]}    audiences spanning different OSM keys
#   {sources: [{type: map|places|directory, …}, …]}   several source adapters
TGT=$(python3 -c "
import json,sys
c=json.load(open('$TPL/sourcing.json'))
srcs=c.get('sources')
if isinstance(srcs,list) and srcs:
    names=[]
    for i,s in enumerate(srcs,1):
        if not isinstance(s,dict) or not s.get('type'): sys.exit('BADSRC:%d' % i)
        if s['type'] not in ('map','places','directory'): sys.exit('BADTYPE:%s' % s['type'])
        if s['type']=='map':
            ts=s.get('targets') or c.get('targets') or ([c] if c.get('selector') else [])
            if not ts: sys.exit('NONE')
            if [i for i,t in enumerate(ts,1) if not (t.get('selector') and t.get('vertical'))]:
                sys.exit('BAD:map source %d' % i)
            names += [t['vertical'] for t in ts]
        elif s['type']=='places':
            ts=s.get('targets') or []
            if not ts: sys.exit('BAD:places source %d has no targets' % i)
            if [t for t in ts if not (t.get('included_type') and t.get('vertical'))]:
                sys.exit('BAD:places source %d needs included_type+vertical' % i)
            names += ['places:'+t['vertical'] for t in ts]
        else:
            names.append('directory')
    print(','.join(dict.fromkeys(names)))
else:
    ts=c.get('targets') or ([c] if c.get('selector') else [])
    bad=[i for i,t in enumerate(ts,1) if not (t.get('selector') and t.get('vertical'))]
    if not ts: sys.exit('NONE')
    if bad:   sys.exit('BAD:%s' % bad)
    print(','.join(t['vertical'] for t in ts))
" 2>&1) || {
    case "$TGT" in
      NONE)     echo "ABORT: $TPL/sourcing.json needs a \"selector\"+\"vertical\" (the OSM tag filter naming this run's target audience, e.g. '\"office\"=\"lawyer\"'), a \"targets\" list for a multi-vertical campaign, or a \"sources\" list."; exit 1;;
      BADSRC*)  echo "ABORT: $TPL/sourcing.json sources entries each need a \"type\" ($TGT)."; exit 1;;
      BADTYPE*) echo "ABORT: $TPL/sourcing.json has an unknown source type ($TGT). Known: map, places, directory."; exit 1;;
      BAD*)     echo "ABORT: $TPL/sourcing.json target entries are incomplete ($TGT)."; exit 1;;
      *)        echo "ABORT: $TPL/sourcing.json is not valid JSON."; exit 1;;
    esac
}
# WHERE: an explicit place list, or countries.txt resolved against the built-in
# table (source_overpass.py aborts loudly if that leaves no places).
if [ ! -s "$TPL/places.txt" ] && [ ! -s "$TPL/countries.txt" ]; then
    echo "ABORT: $TPL needs places.txt (\`City|ISO2|lat|lon|half_width_deg\`, one per line)"
    echo "or countries.txt — Stage 2 sweeps places, so a fixture must declare WHERE it looks."
    exit 1
fi
if [ -s "$TPL/places.txt" ] && ! grep -qE '^[^#].*\|.*\|.*\|.*\|' "$TPL/places.txt"; then
    echo "ABORT: $TPL/places.txt has no real place rows (comments only)."; exit 1
fi

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
# A LinkedIn campaign needs its person gates. Without linkedin.json the run
# would source and qualify companies, walk them to people, and then judge those
# people with nothing but the built-in defaults — wrong titles, wrong countries,
# no geo anchor. Fail here, where the fix is one file, not after 8 minutes.
if [ -f "$TPL/channels.json" ] && grep -q '"linkedin"' "$TPL/channels.json"; then
    if [ ! -f "$TPL/linkedin.json" ]; then
        echo "ABORT: $TPL/linkedin.json missing — a LinkedIn campaign must declare its"
        echo "person gates: {\"countries\":[\"AE\"],\"accept_tiers\":[\"T1\"],\"alive_days\":90,\"title_tiers\":{...}}."
        echo "See templates/gcc-outreach-li/linkedin.json."
        exit 1
    fi
    python3 -c "import json,sys; json.load(open('$TPL/linkedin.json'))" 2>/dev/null || {
        echo "ABORT: $TPL/linkedin.json is not valid JSON."; exit 1; }
    if ! grep -q 'linkedin_angle' "$TPL/pitch.json" 2>/dev/null; then
        echo "ABORT: $TPL/pitch.json has no \"linkedin_angle\" — li-writer needs the ANGLE"
        echo "(what we sell) to compose each DM. LinkedIn never renders body_template."
        exit 1
    fi
fi

if [ "$DRAFT_MODE" = "custom" ] && [ ! -s "$TPL/vertical.txt" ]; then
    echo "ABORT: $TPL/vertical.txt missing/empty — custom runs need an explicit vertical."
    exit 1
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

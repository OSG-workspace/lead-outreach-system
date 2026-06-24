# Run Kickoff

You are the pipeline orchestrator for the Automate lead outreach system. When the user asks to run a campaign, follow these steps exactly. Read this file fully before taking any action.

## Step 1 — Orient

Read `project/PIPELINE.md`. This is the master contract.  
Read `runs/<slug>/run-config.md` (the user will give you the slug, or you create one: `YYYY-MM-DD-<brief-slug>`).  
Read `icp.yaml` and `queries.txt` in the run folder (copy from last run if the brief is the same ICP).

## Step 2 — Create the run folder

```bash
RUN=runs/YYYY-MM-DD-<slug>
mkdir -p $RUN/raw_html
```

If `icp.yaml` and `queries.txt` already exist in the run folder: use them.  
If not: copy from the last run folder (`runs/$(ls runs/ | sort | tail -1)/`).

## Step 3 — Split queries into batches

```bash
split -l 10 $RUN/queries.txt $RUN/queries-batch-
```

Count the batch files: `ls $RUN/queries-batch-* | wc -l`

## Step 4 — Dispatch parallel sourcing agents

Use the `dispatching-parallel-agents` skill.

For each batch file, dispatch one sourcing agent with this prompt (fill in the variables):

```
You are a sourcing agent. Read agents/source-agent.md fully — it is your complete instruction set.

Your query batch (search each query with WebSearch):
<contents of queries-batch-XX>

Output path: runs/<slug>/candidates-batch-<N>.txt

Write your output lines directly to that file as you go.
```

Dispatch ALL agents simultaneously (parallel). Wait for all to complete.

## Step 5 — Merge candidates

```bash
source tools/venv/bin/activate
python tools/scripts/merge_candidates.py \
    --run-dir $RUN \
    --sent-log vault/lead-outreach/sent-log.md
```

Check: `wc -l $RUN/candidates-all.txt` — expect 300–600 lines.  
If fewer than 100: report to user and stop. The sourcing agents may have been too restrictive.

## Step 6 — Fetch HTML

```bash
bash tools/scripts/fetch_html.sh $RUN
```

Watch for: `Fetched N / M → raw_html/`. N should be within 20% of M (some domains will be unreachable).

## Step 7 — Extract leads

```bash
python tools/scripts/extract_leads.py \
    --run-dir $RUN \
    --sent-log vault/lead-outreach/sent-log.md
```

Check: `wc -l $RUN/leads-extracted.json` — expect 50–150 qualified leads.

## Step 8 — Draft emails

```bash
python tools/scripts/draft_emails.py --run-dir $RUN
```

Check: `wc -l $RUN/emails-drafted.json`

## Step 9 — Preview + user approval

Show the user 3 sample emails:
```bash
python -c "
import json
lines = open('$RUN/emails-drafted.json').read().splitlines()[:3]
for l in lines:
    d = json.loads(l)
    print(f'TO: {d[\"to_email\"]}')
    print(f'SUBJECT: {d[\"subject\"]}')
    print(d['body_text'][:300])
    print('---')
"
```

Then ask: **"N emails drafted. Send all N? (yes/no)"**

Do NOT send until the user says yes.

## Step 10 — Send

```bash
python tools/scripts/send.py \
    --run-dir $RUN \
    --send \
    --cap <cap from run-config.md> \
    --pace 30
```

Run in background. Monitor `$RUN/send-log.txt`.

## Step 11 — Persist

```bash
python tools/scripts/persist_sent_log.py \
    --run-dir $RUN \
    --sent-log vault/lead-outreach/sent-log.md
```

Report to user: "Campaign complete. Sent N/M. Sent-log updated."

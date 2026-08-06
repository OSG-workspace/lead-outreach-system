# Lead Outreach System

An agentic cold-outreach pipeline. It finds businesses that match an ICP, proves
who the decision-maker is, writes a personalised email, and sends it — as one
deterministic chain you can re-run, audit, and stop.

The orchestrator is plain Python. It spends **zero LLM tokens on control flow**;
language models are dispatched only for the two jobs that genuinely need
judgement — finding a named decision-maker, and writing copy.

```
source → merge+dedup → fetch → extract+score → qualify+cap
       → enrich decision-maker → draft → send → persist
```

Email is the volume channel. WhatsApp and LinkedIn are opt-in arms of the same
chain, selected per campaign.

---

## What makes it different

**It refuses to send a bad email.** Most of the engineering here is gates, not
generation:

- **A salutation contract.** Every email opens `Hello Mr. <Surname>,` or
  `Hello <First>,` — never `Hello,`, never `<Business> team,`. A lead whose
  decision-maker cannot be resolved is *dropped*, not downgraded to generic.
- **No invented addresses.** A direct email must be verbatim from a real source,
  or reconstructed from an *observed* format with the evidence URL recorded.
  Zero-evidence guessing is refused — it bounces, and bounces cost the domain.
- **Kill-on-fallback.** When a stage degrades, the run halts with a precise root
  cause instead of shipping a weaker version of itself. Fewer, real emails beat
  more, generic ones.
- **Every drop is audited.** `leads-dropped.json` records why each lead died, per
  lead, so a disappointing run is diagnosable rather than mysterious.

**It doesn't repeat itself.** Append-only ledgers make a re-fire safe by
construction: contacted domains, already-sourced domains, retired domains, and
swept map geography are all remembered, so consecutive runs open fresh ground
instead of re-walking old ground.

---

## Cost

Sourcing is **free by design** — no paid enrichment API, no scraping credits:

| Job | Tool | Cost |
|---|---|---|
| Find businesses | OpenStreetMap / Overpass enumeration | free |
| Fetch their sites | `crawl4ai` (JS-rendered, multi-page) | free |
| Web lookups | agent WebSearch / WebFetch | free |
| Send | Brevo | free tier: 300/day |
| Optional wider sourcing | Google Places API (New) | ~$32/1k, opt-in |

**The only credential you need is a Brevo API key.** Everything else is either
free, optional, or degrades cleanly — a campaign with no Places key simply
reports no fresh ground from that source and carries on.

---

## Quick start

```bash
git clone https://github.com/OSG-workspace/lead-outreach-system.git
cd lead-outreach-system

bash tools/git-hooks/install.sh        # guard against committing secrets/PII
bash project/tools/install.sh          # crawl4ai + deps into project/tools/venv

cp project/.env.example project/.env   # add your BREVO_MCP_TOKEN + sender
```

Then fire a campaign. The vault (memory) bootstraps itself on first run:

```bash
cd project
bash tools/scripts/fire_campaign.sh eu-hotels --plan      # trace, consume nothing
bash tools/scripts/fire_campaign.sh eu-hotels --dry-run   # draft, don't send
bash tools/scripts/fire_campaign.sh eu-hotels             # live
```

`--plan` executes nothing and is the right first command: it prints every stage
with its real arguments so you can see the whole chain before spending anything.

**Before your first live send**, write your own `project/vault/lead-outreach/`
docs — `offer.md`, `voice.md`, and a real postal address plus working opt-out in
`compliance.md`. The bootstrap leaves skeletons with instructions; the drafters
read them verbatim, so they are the single biggest lever on quality (and
`compliance.md` is a legal requirement for commercial email, not a nicety).

---

## Campaigns

A campaign is a folder, not code. `project/templates/<name>/` holds its ICP,
sourcing contract, channels, and approved copy; firing clones it into a fresh
dated run folder. 20 ship as examples — hotels, trades, clinics, law firms,
staffing, property, and several regional variants.

Adding one is one command and needs no code change:

```bash
python3 tools/scripts/new_campaign.py --base de-dental --vertical dental \
    --countries DE,AT,CH --selector '"amenity"="dentist"'
```

It scaffolds the structure and then **refuses to fire** until you supply the
things it must not invent for you: your approved email copy (`pitch.json`), real
target geography, and ICP criteria. Copy is never generated at send time — that
is the design's main defence against generic outreach.

---

## Layout

```
.claude/agents/           the 6 sub-agents the orchestrator dispatches
project/
  CLAUDE.md               operating doc: routing, contracts, approval modes
  ARCHITECTURE.md         file + agent map, stage table, gates
  PIPELINE.md             per-stage rationale
  templates/<name>/       one fire-ready fixture per campaign
  tools/scripts/          the deterministic stages (run_fire.py et al)
  linkedin/               the LinkedIn channel (Node + playwright-core)
  bridge/                 the WhatsApp channel (whatsapp-web.js)
  vault/                  YOUR memory — not in this repo, see its README
  runs/<date>-<slug>/     per-run artifacts (gitignored)
tools/git-hooks/          pre-commit guard for secrets + PII
```

---

## What is deliberately not in this repo

**The vault** — sent history, per-lead records, bounce lists, dedup ledgers, and
the live ICP/voice specs. It is contact PII and per-operator state, so it stays
on the operator's machine. `project/vault/README.md` documents its structure and
what each ledger is load-bearing for, and `project/vault-template/` holds the
skeletons the bootstrap installs, so a clone gets a working system without
inheriting anyone's data.

**Run artifacts** — scraped HTML and per-lead records under `runs/`.

The `tools/git-hooks/pre-commit` guard blocks all of it, plus live API keys, even
if `.gitignore` is bypassed with `git add -f`. Install it.

---

## Safety and legality

This sends real email to real businesses. Before firing, understand that:

- **CAN-SPAM** requires a real postal address and a working opt-out in every
  commercial message. `compliance.md` is where you put them.
- **GDPR/UK-GDPR** applies to EU/UK recipients. B2B outreach usually relies on
  legitimate interest — know your basis, and honour objections immediately.
- Suppression, bounce sync, and a per-run send cap are built in and on by
  default. Do not route around them.

Use it on audiences you can defend contacting.

---

## Development

```bash
cd project && python3 -m pytest tests/ -q      # 77 tests, no network required
python3 tools/scripts/name_from_email.py       # self-test for the name parser
```

## License

MIT — see [LICENSE](LICENSE).

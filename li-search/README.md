# li-search — standalone LinkedIn people search

Find decision-makers and output their **LinkedIn account names**, from a target
audience you store once and fire on command.

This tool is deliberately **separate from `project/`**. It shares no code, no
state, no venv, no config and no ledger with the fire chain. Nothing here can
break a campaign run, and a campaign run cannot break this. It is stdlib-only
Python 3 — there is no install step.

```bash
./li-search providers                       # what's configured, what each costs
./li-search define "<audience>" --industry ... --countries ... --cities ...
./li-search list
./li-search fire <slug>                     # -> leads.csv, leads.json, accounts*.txt
./li-search bakeoff <slug>                  # compare providers on YOUR market
./li-search export <brief> --audiences a,b  # deliver the next batch into results/<brief>/
```

## The line this tool will not cross

**It never contacts linkedin.com, and it never drives a logged-in LinkedIn
session.** That is enforced in [`lisearch/compliance.py`](lisearch/compliance.py)
and covered by tests, not left to discipline:

- every outbound URL is checked, and a linkedin.com host raises rather than fetches;
- any adapter carrying a session cookie (`li_at`, `JSESSIONID`, …) is refused at
  registration.

The reason is the 2026 record. LinkedIn's User Agreement §8.2 bans scraping and
also bans using data obtained *through aggregators* without consent; LinkedIn v.
Nubela (N.D. Cal. 3:25-cv-00828) ended in a permanent injunction and shut
Proxycurl down at roughly $10M ARR; and the community LinkedIn MCP servers all
drive **your** account at machine cadence, which is the pattern that gets
individual accounts restricted. Session-driven access buys recall by putting the
operator's own account on the table. This tool declines that trade.

There is also no official route: SNAP is closed to new partners, Talent
Solutions is ATS-partner-only, and **no LinkedIn API grants people search**. So
the choice is authorised third-party index or nothing — and it is the index.

## Providers

Priority order is the fire order. **By default a fire runs every provider that
can run right now** — keyed ones only when their key is set, the two free ones
always. With no keys at all, `ddgs` and `openweb` still fire.

| provider | kind | why it's here |
|---|---|---|
| **exa** | authorised index | Recommended keyed primary. Its open benchmark reports the right person at rank 1 on ~72–75% of queries vs 20.8% (Parallel) / 40.5% (Tavily). Vendor-authored — treat the exact numbers with suspicion, but the direction is the brief's core claim: the win comes from a **specialised index**, not from touching LinkedIn. No account risk. |
| **coresignal** | licensed DB | Alternative primary when you want bulk structured rows + firmographics. Cheapest per-record at volume (~$0.005). Two-call shape (search → collect) is the cost model, so collects are capped and cached. |
| **pdl** | licensed DB | Enrichment fallback. Real cursor, complete rows — but ~260M of its records are US and email fill skews US/UK/CA, so it is a fallback for a MENA-first operator, not a foundation. |
| **ddgs** | free multi-engine index | **The free workhorse.** Rotates Bing, Google, Yahoo and Yandex through the `ddgs` library, so one engine throttling costs a little recall instead of all of it. Needs *some* interpreter that can `import ddgs`: it looks at `config.ddgs.python`, `LI_SEARCH_DDGS_PYTHON`, python3 on PATH, then `../project/tools/venv` (which has it). Skipped with the reason printed if none can. |
| **openweb** | keyless stdlib fallback | One engine's HTML page (DuckDuckGo), stdlib only. Throttles easily — it answers a disliked client with an HTTP 202 interstitial for a while — so it is last, and the interstitial is detected and reported rather than read as an empty market. |

The web-index providers all fire the same query matrix
(`lisearch/adapters/base.py: web_queries`):

```
site:ae.linkedin.com/in "Owner" "real estate" Dubai
```

When a brief NAMES firms and founders, seed them: `--companies "Microbits,Bluemoon"`
and `--people "Kaysar Daou @ Leoceros,Rola Ghotmeh @ The Creative 9"`. Write a
person as `Name @ Firm`: the firm is what stops a common name from qualifying
every namesake — a bare-name seed qualifies only when the row itself shows a
title or industry match, and otherwise appears as `person?=` (candidate). Each
seed becomes a query fired before the matrix (`site:lb.linkedin.com/in "Microbits" (Founder OR Owner OR
CEO …)`, `site:linkedin.com/in "Kaysar Daou" Lebanon`). A row whose name is a
seeded person is qualified outright (`match=person=…`); a seeded company in the
row counts as industry evidence. Keep generic-word firm names (Maze, Ethos,
Spirit) out of the seed list — they false-match — and reach them through the
founder name instead.

The **country subdomain** is the geo filter — LinkedIn serves a member's public
profile from the subdomain of their own stated location, which an index honours
far more reliably than the word "Dubai" in a headline. One subject per query
(industry first, then each keyword), tiered so a small query cap still covers
every city × every top title before spending queries on synonyms. `ddgs`
reads three result pages per query by default: measured on one Dubai query,
pages 2 and 3 each added as many new profiles again as page 1 held, while a
second engine added none.

A Brave Search API key in the environment is **not** wired in on purpose: the
quota headers on this operator's key show a metered plan with no free monthly
allowance, and this repo is free-only.

Adding a provider is one file in `lisearch/adapters/` plus a line in the
registry. That is the point: Proxycurl went from $10M ARR to dead in six months,
so a vendor disappearing is the **base rate** in this category, not the tail.

## Cost, and this repo's free-only rule

`project/CLAUDE.md` requires a free-only stack. This tool honours that by
**defaulting to zero spend**: with no keys set, the default fire runs only the
two free providers and costs nothing. Adding a key is an explicit opt-in to
that provider's pricing.

Guards against surprise spend:
- `--dry-run` plans a fire and fetches nothing;
- responses are cached with a 30-day TTL, so re-firing an audience is nearly free;
- `coresignal.max_collects` is a hard ceiling on credits per fire;
- a 402 from any provider stops that provider rather than looping.

## Provenance and erasure

Every record carries `sources`, `collected_at`, `freshness_days` and a coarse
`freshness` flag (`live` / `fresh` / `aging` / `stale` / `unknown`). Batch
providers are reported honestly as `unknown` or `aging` rather than dressed up as
live — several serve rows that are 3–4 months old at access.

`eu_flag` marks EEA/UK records. GDPR Art. 3(2) reaches a Lebanon-based operator
processing EU-resident data, Lebanon has no adequacy decision, and Art. 14 notice
is the most common enforcement failure (Kaspr, CNIL SAN-2024-020, €240k). The
flag tells you which rows carry that exposure; it does not discharge it.

`./li-search suppress <account|url|email>` adds someone to `suppression.txt`,
applied on every future fire — so an erasure request is one command in one place.

## Before you commit to a vendor

```bash
./li-search bakeoff <slug> --sample 50
```

Runs your audience through every configured provider separately and reports rows,
name/company/email fill, and mean score. **No published benchmark answers the
MENA question** — coverage there is qualitative and every major provider is
North-America-heavy. Drop any provider under ~50% usable recall on your own
market regardless of its headline score.

## Output

`runs/<date>-<slug>/`:
- `leads.csv` — spreadsheet-ready, with `qualified` and `match` columns
- `leads.json` — full records + provenance + run stats
- `accounts.txt` — every LinkedIn account name, one per line
- `accounts-qualified.txt` — only the accounts that meet the full specification
- `audience.json` — the exact definition fired, so a run is reproducible

**Qualified** means the row shows, in its own text, all three parts of the
stored specification: an owner-equivalent title (whole-word, and not "Vice
President" / "Assistant to the CEO"), the target geography (city in the text,
or the country from the profile's LinkedIn subdomain), and the target industry.
`match` names which parts hit (`title=Owner;geo=city;industry=real estate`), so
a near-miss is one glance away from being promoted. Rows that miss a part are
kept and ranked below the qualified ones, never dropped — the operator asked for
as many leads as possible.

**A fire is cumulative.** Every earlier run of the same audience is folded into
the new one (deduped by account, provenance unioned, gaps filled), so firing on
a day one engine is throttled never shrinks the pool. The summary line tells you
how many accounts are genuinely new this fire.

## Delivering: `export`

Run folders are provenance. What the operator keeps is `results/<brief>/`:

```bash
./li-search export shughol-lebanon --audiences lb-marketing-creative-agency-owners,lb-pr-events-production-owners,lb-consultancy-advisory-partners,lb-dev-shops-ai-builders --take 50 --table
```

- `owners.csv` — every qualified account ever delivered for the brief, numbered
  and never repeated across batches; brief-named (seeded) rows come first.
- `batch-NN.csv` — each delivery exactly as handed over.
- `accounts.txt` — the account names alone.

Re-fire the audiences, run `export` again, and only the genuinely new
qualified accounts are appended as the next batch.

## Token discipline (for a Claude session running `/li-fire`)

The tool itself spends **zero LLM tokens** — no agents, no model calls, pure
Python — so the only token cost of a LinkedIn run is what a session reads back.
The output is shaped to keep that small, the same way the email chain's
`run_fire.py` is:

- `./li-search fire <slug> -q` prints two lines: a `DONE:` line
  (`kept= qualified= new= unique= excluded= providers=…`) and the run dir.
- `runs/<id>/summary.json` holds every run stat plus a 10-row preview, and
  **no lead rows** — read this, never `leads.json`.
- `runs/<id>/status.txt` is a one-line heartbeat overwritten at each stage and
  ending in the `DONE:` line, for a session checking on a fire from elsewhere.
- The default (non-quiet) output previews 10 rows; `--top N` widens it, and the
  files always hold everything.
- Provider messages are one line each; a throttled engine says so in one line.

## Tests

```bash
python3 -m unittest discover -s tests -v      # 42 tests, no network, no keys
```

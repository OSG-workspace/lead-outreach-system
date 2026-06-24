---
name: lead-sourcing-maps
description: Sources business leads from Google Maps using the free, open-source gosom/google-maps-scraper Go binary (no API key, no limits). Use when the run's ICP includes google_maps as a source. Outputs structured leads with names, addresses, phones, websites, ratings, review counts, and emails (when -email flag is enabled).
---

# Google Maps Sourcing (Free Stack)

This skill uses **`gosom/google-maps-scraper`** — a free, open-source Go binary that scrapes Google Maps with no API key and no rate limits. It's the best free Maps tool available (3.3k stars, MIT license, ~120 places/minute, 33+ data points per place).

The binary is installed by the `setup-tools` skill at `./tools/google-maps-scraper` (path stored in `$GMAPS_SCRAPER_PATH`). If the binary doesn't exist, run `setup-tools` first.

## How to invoke it

The scraper takes a queries file (one query per line) and outputs CSV or JSON.

### 1. Build the queries file

Create `runs/<run-slug>/queries.txt` with 1-5 search queries derived from the ICP. Construct queries like a real user would type:

```
dentist in Beirut Lebanon
dental clinic Beirut
pediatric dentist Beirut Hamra
```

More queries = more leads. Default to 3 well-chosen queries. Avoid duplicates — the scraper handles dedup internally on `place_id`.

### 2. Run the scraper

For most runs (~60 leads, with email extraction):

```bash
$GMAPS_SCRAPER_PATH \
  -input runs/<run-slug>/queries.txt \
  -results runs/<run-slug>/maps-raw.json \
  -json \
  -email \
  -depth 1 \
  -c 4 \
  -lang en \
  -exit-on-inactivity 3m
```

Key flags:

- `-json` — JSON output (easier to parse than CSV)
- `-email` — visit each business website to extract emails (REQUIRED for cold email use case; adds time)
- `-depth 1` — how many "more results" pages to scroll. 1 = ~60 results per query. Bump to 2-3 for larger runs.
- `-c 4` — concurrency. 4 is safe; bump to 8 for faster runs on a beefy machine.
- `-exit-on-inactivity 3m` — exit cleanly when no new leads for 3 minutes
- `-lang en` — change to local language code if targeting non-English regions (`fr`, `ar`, `de`, etc.)

For geo-tight searches, add:

```bash
  -geo "33.8938,35.5018" \   # Beirut center coords
  -zoom 12 \                  # 0-21, lower = wider area
  -radius 5000               # meters
```

For fast mode (up to 21 results per query, no email enrichment, faster):

```bash
  -fast-mode
```

### 3. Filter and trim — DO NOT read raw output into context

The raw scraper output is 1-3KB per record × 60-200 records. Reading all of that into Claude's context just to map fields is the single biggest token waste in this skill. Instead, run the filter script:

```bash
./tools/run.sh tools/scripts/filter_maps_leads.py \
  --input runs/<run-slug>/maps-raw.json \
  --output runs/<run-slug>/leads-raw.json \
  --city "<primary city from ICP>" \
  --min-rating 3.0
```

The script does all of the following in pure Python — no Claude tokens:

- Maps each scraper record to the slim canonical lead schema (below)
- Picks the highest-quality email per lead (named > function > generic, drops `noreply@` etc.)
- Detects chains (well-known brand names + ≥4 related places)
- Drops closed/permanently-closed/temporarily-closed listings
- Drops `rating < min-rating` with > 5 reviews
- Drops listings whose address doesn't contain the target city
- Drops listings with no email AND no website AND no phone

It prints a one-line JSON summary (`{"input": 180, "kept": 53, "dropped": {...}}`) — that's all Claude needs to see.

Optional flags:

- `--require-website` — skip leads that don't have a website (only do this if your ICP's gap is website-related)

After this step, `leads-raw.json` is the slim canonical-schema JSONL. Only read THAT file into context, never `maps-raw.json`.

### Canonical lead schema (output of the filter)

```json
{
  "id": "maps-<slug>",
  "name": "<title>",
  "source": "google_maps",
  "url": "<website or link>",
  "email": "<best-ranked email or null>",
  "phone": "<phone>",
  "location": "<complete_address>",
  "raw": {
    "place_id": "<place_id>",
    "rating": <review_rating>,
    "review_count": <review_count>,
    "category": "<category>",
    "is_chain": <bool>,
    "all_emails": [...],
    "footer_year": <int or null>,
    "google_maps_url": "<link>"
  }
}
```

If you need additional fields (open_hours, lat/lng, owner_claimed, etc.) for a specific lead, fetch them from `maps-raw.json` by `place_id` on demand — don't load the whole file.

Keep leads with no email — gap analysis can extract emails from their website later.

## Cost & speed

- **Free.** No API key. No quota.
- ~120 places/minute baseline. With email extraction, drop to ~40-60/min (the scraper visits each website).
- 60 places × 3 queries = 180 leads in roughly 5-10 minutes with email extraction on.
- Memory: ~500MB while running (uses Playwright headless Chrome internally).

## Failure modes

- **Binary not found**: run `setup-tools` skill to install
- **Zero results**: queries are too narrow. Try broader (drop city qualifier, e.g., "dentist Beirut" instead of "pediatric dentist downtown Beirut Hamra")
- **Google bot-block** (rare on personal IPs): pause for 30 minutes; if persistent, the binary supports proxies via the `-proxies` flag (free public SOCKS5 proxies usually work for low volume)
- **Crashes mid-run**: the scraper supports resuming via `-dsn` with PostgreSQL, but for personal use just re-run with `-exit-on-inactivity 1m` to fail fast and retry
- **Email extraction returns nothing**: some businesses genuinely have no public email. Their website might also use contact forms only. Note this and let gap analysis handle it.

## Anti-patterns

- Don't run this in parallel with itself (multiple instances on the same IP get blocked faster).
- Don't set `-c` higher than 8 unless you know your network can handle it.
- Don't disable `-exit-on-inactivity` — without it the binary can hang forever waiting for results that aren't coming.

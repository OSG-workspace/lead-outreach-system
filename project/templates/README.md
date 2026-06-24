# Campaign templates — the FIXED source of truth

Each folder here is the **canonical config for one campaign**. A fire trigger
clones `templates/<base>/` into a new dated `runs/<date>-<base>/` and launches it.

**The orchestrator reads ONLY from here — never from previous runs.** This is
deliberate: firing is direct and reproducible, with no "look at the latest run to
figure out the config" step. To change a campaign (queries, ICP, voice, channels,
qualify gate), **edit the fixture in this folder** — not a past run under `runs/`.

| Fixture | Campaign | Source agent | Draft mode | Channels |
|---|---|---|---|---|
| `us-law-firms/` | US law firms | source-agent-us | custom | email |
| `us-staffing/` | US staffing/recruiting | source-agent-us | custom | email |
| `us-clinics/` | US med spas / dental / clinics | source-agent-us | custom | email |
| `us-property/` | US property management | source-agent-us | custom | email |
| `gcc-auto/` | GCC consumer chains | source-agent | template | email |
| `eu-hotels/` | Big hotels ES/IT/FR (AI receptionist) | source-agent-worldwide | template | email |
| `lb-enterprise/` | Biggest Lebanese companies | source-agent-lb-enterprise | custom | whatsapp |
| `lb-receptionist/` | Lebanon phone-heavy SMBs | source-agent-lb | template | email+whatsapp |
| `worldwide-receptionist/` | Worldwide AI receptionist | source-agent-worldwide | template | email |

A fixture must contain at least `icp.yaml` + `queries.txt`. Other control files
(`source_agent.txt`, `draft_mode.txt`, `channels.json`, `countries.txt`,
`pitch.json`, `qualify.json`, `targeting.md`, `brief.md`, `enrich_cap.txt`) are
copied if present. The routing phrase → fixture map lives in `../CLAUDE.md`.

# Despatch and Demurrage

Grafana dashboard for weighbridge ticket data across multiple port sites:
trucks crossing a weighbridge (empty + laden weights) either loading a
vessel for dispatch (`OUT`) or unloading one as demurrage (`VESSEL-IN`).

## Architecture

```
Excel exports (.xlsx, one per site) --[load_data.py]--> Postgres --[Postgres data source]--> Grafana
```

This repo does **not** solve ongoing ingestion — it's a repeatable load of
ticket exports into a queryable store, plus a dashboard built on that
schema. If exports start arriving on a schedule, that's a separate piece
of work (a cron job / watcher calling `load_data.py` on each new file).

Postgres was chosen over reading the .xlsx/CSV directly in Grafana because
Grafana has no first-class spreadsheet support, and community CSV/Sheets
plugins re-scan the whole file per query with no indexing, dedup, or
schema enforcement. This data model (weighbridge tickets, keyed by
`(site, ticket)`) benefits from a real primary key and `ON CONFLICT`
upsert so re-running the loader on an overlapping export never creates
duplicate rows.

## Setup

Full walkthrough, including the Grafana token and troubleshooting:
**[SETUP.md](SETUP.md)**. The short version, once Grafana is running:

```bash
docker compose up -d                       # Postgres
pip install -r requirements.txt            # loader dependencies
python load_data.py /path/to/site1.xlsx /path/to/site2.xlsx ...
cp .env.example .env                       # then paste a Grafana service account token in
python generate_dashboard.py && python push_dashboard.py despatch-and-demurrage.json
python generate_live_dashboard.py && python push_dashboard.py live-operations.json
```

Before pushing, add the Postgres data source in Grafana and check its UID
matches the generators (SETUP.md step 5).

### Files

| File | What it is |
|---|---|
| `load_data.py` | Loads site `.xlsx` exports into Postgres (`tickets`), and seeds `bay_capacity` |
| `generate_dashboard.py` → `despatch-and-demurrage.json` | Historical, multi-voyage trend dashboard |
| `generate_live_dashboard.py` → `live-operations.json` | Live operations status board |
| `push_dashboard.py` | Validates a dashboard's queries against Grafana, then deploys it |
| `docker-compose.yml` | Postgres 16 |
| `postgres.yml`, `dashboards.yml` | Grafana provisioning files for a containerised Grafana (SETUP.md) |
| `.env.example` | Template for `.env` (Grafana URL + token, gitignored) |

The dashboard JSON files are generated: change the generator and re-run
it, never hand-edit the JSON.

## Data model

Source columns (one row = one weighbridge crossing), as exported per site:

| Column | Meaning |
|---|---|
| `Site` | Site code (`DUB`, `FOY`, `LIS`, `RIN`, ...) |
| `Weighticket` | Ticket ID — unique **within a site**, not globally (primary key is `(site, ticket)`) |
| `Voyage` | Voyage ID. Voyages don't collide across sites. |
| `Weighticket type` | `OUT` (dispatch — loading a vessel) or `VESSEL-IN` (demurrage — unloading one) |
| `Vehicle Num` / `Trailer Num` | Truck / trailer registration |
| `Date+Time` / `Date+Time2` | Weighbridge crossing window (in / out) |
| `Weight` / `Weight2` | Weighs either side of tipping |
| `Weight3` (or `Weight4` at Foynes) | Net weight as reported by the source system |
| `Loading/Tipping Location` | Where the truck is filled or where it delivers, depending on ticket type |
| `Item number` | Product/commodity code |

`load_data.py` derives:
- `net_weight` = `abs(Weight - Weight2)`, computed independently rather
  than trusting the source `Weight3`/`Weight4` column — see Data quality below.
- `weight_mismatch` = `abs(net_weight - net_weight_reported)`.
- `cycle_seconds` = `abs(Date+Time2 - Date+Time)`.
- `cycle_time_reversed` = whether `Date+Time2` was actually before
  `Date+Time` in the source (see Data quality below) — the correction
  applied to get `cycle_seconds` stays visible via this flag rather than
  disappearing silently.

**Schema quirk:** Foynes' export names the net-weight column `Weight4`
instead of `Weight3`. `load_data.py` detects and normalizes this per file.

## Dashboard

19 panels. Three filter variables — **site**, **voyage** (chained: its
option list narrows to whichever sites are selected, and to voyages that
were in progress at some point during the picked time range), **ticket
type** (`OUT` / `VESSEL-IN`) — apply to every panel.

- **KPIs**: total net weight, split into dispatch (OUT) and demurrage
  (VESSEL-IN) totals, total crossings, active trucks, avg cycle time.
- **Throughput over time**: net weight per crossing and cumulative net
  weight, both colored by ticket type so the two cargo flows read as
  distinct series rather than blending into one meaningless line.
- **Per-site breakdown**: net weight, crossings, avg cycle time by site.
- **Per-truck breakdown**: same three metrics plus avg net weight per
  tip, scoped to the **Top 15 busiest trucks** within the current filter
  (see "Top-N trucks" below).
- **Cycle-time outlier tracking**: per-crossing cycle time, flagged past
  30 minutes.
- **Weighbridge activity timeline**: Top-15-trucks only, same reason as above.
- **Net weight & cycle time per tip, by truck**: dual-axis time series,
  Top-15-trucks only.
- **Data Quality Flags**: table of every ticket tripping any of the four
  checks below.

### Top-N trucks

Initial load spans 17-113 distinct vehicles per site (Dublin alone has
113). A bar or timeline series per truck past a handful is unreadable, so
every per-truck panel ranks trucks by crossing count **within the current
filter** and keeps only the busiest 15 (`TOP_N` in `generate_dashboard.py`
— change it and re-run the script to adjust). This means the Top-15 list
itself changes as you filter by site/voyage/ticket type, which is
intentional: "busiest trucks in this voyage" and "busiest trucks overall"
are different questions.

### Data quality flags

Four independent checks, each grounded in what's actually in the initial
load (7,056 rows across Dublin/Foynes/Lisahally/Ringaskiddy):

1. **`weight_mismatch > 0.01`** — the source's reported net weight
   (`Weight3`/`Weight4`) doesn't reconcile with `abs(Weight - Weight2)`.
   9 rows, all at Foynes.
2. **`cycle_minutes > 30`** — recalibrated against the full combined
   dataset: median cycle time ~7.1 min, p75 ~9.3 min, p90 ~13.9 min,
   p95 ~19.7 min. 30 min flags 179/7056 rows (~2.5%) — a genuine long
   tail, not a broad swath of normal tickets.
3. **`net_weight < 10`** — partial/small load. Flags 40 rows. May be
   genuine (a final top-up load, a short haul) rather than an error —
   surfaced for review, not auto-corrected or excluded.
4. **`cycle_time_reversed`** — `Date+Time2` was before `Date+Time` in the
   source. Rare and scattered (23 rows total) at Dublin/Foynes/Lisahally/
   Ringaskiddy. **Does not** catch Lisahally's separate pattern below.

**Lisahally-specific note, not caught by flag #4:** ~32% of Lisahally's
895 rows (289) have `Date+Time2 == Date+Time` — zero cycle time, not a
reversal. This is concentrated almost entirely at one site, which points
to a logging limitation in that site's weighbridge system (it may only be
capturing one timestamp per ticket) rather than scattered data corruption.
It's a site-level characteristic worth knowing about, not something the
per-ticket flags surface — check `cycle_seconds = 0 AND site = 'LIS'`
directly if you need to quantify it.

## Notes

- `.gitignore` excludes `*.xlsx`/`*.csv` — raw exports contain commercial
  data (tonnages, voyage detail) that shouldn't live in a public repo
  history. Load your files locally; don't commit them.
- `generate_dashboard.py` builds the dashboard JSON from code rather than
  the JSON being hand-edited, so panel/query changes are reviewable as a
  diff to Python, not a diff to a multi-thousand-line JSON blob. Edit that
  script and re-run it to change the dashboard, rather than hand-editing
  the generated JSON.
- **Datasource UID**: `generate_dashboard.py` hardcodes the real Postgres
  data source UID (`afxbuc9yby1ogd`) rather than using Grafana's
  `__inputs`/`__requires` import-template placeholder mechanism. That
  mechanism turned out not to reliably substitute across every panel and
  template-variable location on import in practice — panels ended up
  pointing at a nonexistent `${DS_POSTGRES}` datasource after import,
  requiring a manual fix. A hardcoded UID is more reliable, at the cost of
  needing a one-line edit (`DS = {...}` near the top of the script) if you
  ever recreate the Postgres data source under a different UID.
- **Old sample-schema migration**: the very first version of this loader
  used a `tickets` table shaped around the single-site `VesselSample.xlsx`
  placeholder. `load_data.py` now detects that old schema (missing a
  `site` column) and drops it automatically, once, the first time it runs
  against the new schema — see the comment above `DROP_OLD_TABLE_SQL` in
  that file. This never touches real site data loaded under the current
  schema.

## Live operations dashboard

`generate_live_dashboard.py` builds a **second, separate** dashboard
(`live-operations.json`) modeled directly on a "Ringaskiddy Discharge
Operations" status-board mockup — a live, single-voyage snapshot rather
than the historical multi-voyage trend view `despatch-and-demurrage.json`
provides. Both dashboards stay in the repo; this doesn't replace the
other one. Deploy it with `push_dashboard.py` (SETUP.md step 8); a hand
import works too, minus the per-voyage truck filtering described below.

Originally a single-voyage board; `voyage` is now multi-select like the
other filters, defaulting to **All voyages** currently in scope. Narrow
it to one voyage the way you'd narrow any other filter. Auto-refreshes
every minute. With "All" selected, the header (Site/Vessel Name) rolls
up across every selected voyage (comma-separated / aggregated) rather
than assuming exactly one.

**Filters**: `site`, `voyage`, `ticket_type`, `bay`, and `vehicle`, all
chained (each narrows the ones below it: site → voyage; site + voyage →
bay; site + voyage + ticket_type + bay → vehicle) and all defaulting to
"All" so nothing is excluded until you narrow one. Not every filter
touches every panel — see the per-panel notes below.

The `voyage` list also follows the time picker: a voyage is listed only
if its span (first ticket opened → last ticket closed) overlaps the
picked range, so a "today" window lists only voyages active today, and
an empty list means nothing was running in that window.

**Panels**, top to bottom:
- Header (site, vessel name, commodity) — voyage-scoped only, not
  affected by the other filters. Built as `table` panels with the header
  row hidden (not `stat` panels) — see the note below on why.
- **Per-voyage truck sections** — a "Voyage …" section per voyage
  active in the picked window, each holding one line per truck that
  worked **that voyage** in the window, two panels side by side:
  - a summary panel titled with the licence plate, showing 4
    background-coloured tiles (tons moved in the window, avg tons/hr,
    avg tips/hr, minutes since last tip). Tons has no natural good/bad
    band, so it's a flat blue; the other three use a 3-band
    (red/amber/green) version of the Truck Performance table's
    thresholds — see Thresholds.
  - a time series of that truck's tips: each tip's net weight as a bar,
    and the running total as a line on the right axis.

  Classic Grafana repeats can't nest (a truck repeat inside a voyage
  repeat lists every truck in every voyage), so this relies on Grafana's
  newer dashboard layout (`dashboardNewLayouts`, on in this instance),
  where a row can carry its own variables that are re-queried per
  repeated copy. `push_dashboard.py` uploads the classic JSON, then moves
  the hidden truck-list variable onto the voyage row through the v2
  dashboard API (driven by the `x-row-variables` key in the generated
  JSON). **Import the JSON by hand instead and the board still works,
  but each voyage section lists every active truck**, with "–" tiles for
  trucks not on that voyage — use the script.
The sections below the voyage sections sit under a single
"Trucks, stores & vessels" heading:
- **Truck Performance** table — tons/tips/rate today vs. for the whole
  voyage, colour-coded against thresholds (see below). Honours all five
  filters.
- **Truck Tips Through Time** — a time series of every individual tip
  (net weight per crossing), one line per truck. Honours all five
  filters; narrow the vehicle filter if a busy site makes this crowded.
- **Store (by bay)** table and its bar-gauge equivalent (**Store Bay
  Fill**) — per store: name, storage used, max capacity and % full
  (a gauge column in the table; each bar labelled
  `store · used / max t`). Grouped by site and bay, since some bay codes
  exist at more than one site. Always
  `VESSEL-IN` only (that's what "Store" means here) regardless of the
  `ticket_type` filter; honours site, vehicle and bay.
- **Vessel (loading)** table and its bar-chart equivalent (**Vessel
  Loading**) — per-vessel loaded tonnage, no hatch split. Always `OUT`
  only regardless of the `ticket_type` filter; honours site and vehicle,
  not bay (a vessel's location field is a vessel name, not a bay code).

**Why the header tiles are `table` panels, not `stat` panels**: every
`stat` panel elsewhere on this board aggregates to a plain number (a
sum, count, or average). Site/Vessel Name/Commodity aggregate to *text*
(`string_agg`, `regexp_replace`) — and in this Grafana instance, `stat`
panels showed "No Data" for a text-only result even though the identical
filtering logic works fine in a `table` panel (confirmed against the
Truck Performance table, which mixes text and numeric columns and
renders correctly). Rather than chase the exact cause inside Grafana's
Stat panel internals, these three were switched to `table` panels with
the header row and grid lines hidden, which reads the same as a stat
tile but is proven to work here.

### Gaps versus the original mockup, and how they're handled

The mockup asked for more than the weighbridge export actually contains.
Rather than fake data to fill the gaps, each one is either downgraded to
what's actually derivable or left visibly incomplete:

- **No hatch numbers anywhere in the source data.** The mockup's "Vessel"
  table is a per-hatch breakdown of cargo loaded onto a vessel (dispatch
  / `OUT` tickets). None of the four site exports record a hatch number
  — `OUT` tickets only give a vessel name (parsed out of the
  `Loading/Tipping Location` field, which for `OUT` tickets reads like
  `"EQUINOX EAGLE - VOY.000549"`). The Vessel table is downgraded to a
  per-**vessel** rollup (tons loaded, tip count) with no hatch split.
- **No bay/vessel capacity data anywhere in the source data** — this is
  a fixed physical number (how big is bay S1) that a transaction log
  never records. `load_data.py` seeds a `bay_capacity` table
  (`site, bay, max_capacity, store_name`) with a **placeholder default of 5000t** the
  first time each `(site, bay)` pair is seen in a `VESSEL-IN` ticket
  (`ON CONFLICT DO NOTHING`, so a value you've since corrected is never
  overwritten by a reload). Edit it directly in Postgres with real
  figures:
  ```sql
  UPDATE bay_capacity SET max_capacity = 4800, store_name = 'South Store 1'
  WHERE site = 'RIN' AND bay = 'S1';
  ```
  Store names aren't in the exports either (only bay codes), so
  `store_name` starts empty and the dashboard shows the bay code until
  you set one. The Store table shows store, storage used, max capacity
  and a fill gauge; the fill bar chart labels each bar
  `store · used / max t`. Until real capacities are entered, busy bays
  can read **over 100%** full — that's the placeholder telling you it's
  wrong, not a bug. There's no equivalent capacity figure for
  the Vessel table (a vessel's total hold capacity), so it only shows
  tons loaded, not a remaining-capacity column.
- **Most voyages have no vessel name available at all.** Vessel name is
  only derivable from `OUT` tickets' location field. Most voyages in the
  initial load are pure discharge (`VESSEL-IN` only, confirmed against
  the real data — e.g. 8 of Ringaskiddy's 11 voyages have zero `OUT`
  tickets), so for those the header falls back to a plain note rather
  than guessing a name from nothing.
### Point-in-time reconstruction

The dashboard's time range picker (top-right corner) isn't just for
scoping the "today" columns — it drives the whole board. Set its **end
("To")** to a moment in the past and every panel reconstructs what the
board would have shown at that moment, not what it looks like now:

- **Truck Performance**'s "vessel total" columns (`Tons (vessel)`,
  `Avg t/hr (vessel)`) and **Last Tip (mins)** only count tickets up to
  the picked "To" time, and "Last Tip" is measured from that time, not
  from real wall-clock `NOW()`.
- **Store (by bay)** and **Vessel (loading)** only sum tickets up to the
  same "To" time.
- The "today" columns (`Tons today`, `Avg t/hr today`, `Tips/hr today`)
  already respected the full picked range (`$__timeFilter`); that
  behaviour is unchanged.

Leave the time range at its default (ending "now") and this behaves
exactly like a live board — `$__timeTo()` just resolves to the actual
current time. To look at a finished historical voyage as it stood at a
specific hour, first set the time range (the `voyage` dropdown only
lists voyages active in the picked window), then pick that voyage —
e.g. a range like `2026-06-14 00:00:00` to
`2026-06-14 14:30:00` — the board shows exactly what would have been on
screen at 14:30 that day, including a truck's "last tip" gap measured
from that same 14:30, not from today.

### Thresholds

Colour thresholds on the Truck Performance table are calibrated against
the real combined dataset (178 truck-voyage groups with a usable
activity span), **not** copied from the mockup's illustrative numbers,
which are specific to one example vessel/commodity and aren't a general
target:

| Metric | Red when | Basis |
|---|---|---|
| Avg Tons/hr (today & vessel total) | < 25 t/hr | ~p25 of real tons/hr (p10 4.7, p25 21.1, p50 49.1, p75 77.3, p90 114.0) |
| Avg Tips/hr (today) | < 1.0 tips/hr | ~p25 of real tips/hr (p10 0.13, p25 0.8, p50 1.76, p75 2.99, p90 3.67) |
| Last Tip (mins) | > 60 min | sits between p90 (50.4 min) and p95 (77.4 min) of real inter-tip gaps |

The tiles on each truck's summary panel use a 3-band (red/amber/green)
version of these same three, with the median as the amber→green
breakpoint (so "green" means at/above what a typical truck actually
does, not merely "not terrible"):

| Tile | Red when | Amber when | Green when |
|---|---|---|---|
| Avg Tons/hr | < 25 t/hr | 25–49 t/hr | ≥ 49 t/hr (p50) |
| Avg Tips/hr | < 1.0 tips/hr | 1.0–1.76 tips/hr | ≥ 1.76 tips/hr (p50) |
| Last Tip (mins) | ≥ 60 min | 30–60 min | < 30 min |

One further threshold, calibrated by construction rather than against
the dataset's own percentiles (there's no equivalent "real" distribution
for a fill percentage — it's relative to whatever bay capacity is on
screen):

| Panel | Amber when | Red when | Basis |
|---|---|---|---|
| Store Bay Fill (% of capacity) | ≥ 80% | ≥ 100% | standard "getting full" / "over capacity" bands; only as meaningful as the underlying `max_capacity` figure — see the placeholder-capacity note above |

These are starting points based on what this initial dataset actually
looks like, not calibrated operational targets — adjust the constants
near the top of `generate_live_dashboard.py`
(`TONS_PER_HR_RED_BELOW`, `TIPS_PER_HR_RED_BELOW`,
`LAST_TIP_RED_ABOVE_MIN`, `TONS_PER_HR_AMBER_TO_GREEN`,
`TIPS_PER_HR_AMBER_TO_GREEN`, `LAST_TIP_AMBER_AT_MIN`,
`BAY_FILL_AMBER_AT`, `BAY_FILL_RED_AT`) once
you know what "good" actually means for your operation, and re-run the
script.

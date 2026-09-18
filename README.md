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

```bash
# 1. Start Postgres
docker compose up -d

# 2. Install Python deps
pip install -r requirements.txt   # add --break-system-packages on Linux
                                   # distros with a "managed" system Python

# 3. Load ticket exports — one or more files, one per site.
#    Safe to re-run: rows are upserted on (site, ticket).
python load_data.py /path/to/dublin_august_2026.xlsx \
                     /path/to/foynes_august_2026.xlsx \
                     /path/to/lisahally_august_2026.xlsx \
                     /path/to/ringaskiddy_august_2026.xlsx

# 4. In Grafana: Connections -> Add new connection -> PostgreSQL
#    host: localhost:5432   db: vessel   user: vessel   password: vessel
#
# 5. Import the dashboard: Dashboards -> New -> Import ->
#    upload grafana/dashboards/despatch-and-demurrage.json
```

The dashboard JSON has the Postgres data source's UID hardcoded (not an
import-time placeholder) — see "Datasource UID" below for why, and what
to do if you ever recreate the data source under a different UID.

`grafana/provisioning/` contains datasource + dashboard auto-provisioning
config for a *containerized* Grafana (mount it into `/etc/grafana/provisioning/`).
If you're running Grafana natively rather than in Docker, step 4/5 above
(manual add + import) is the simpler path.

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
option list narrows to whichever sites are selected), **ticket type**
(`OUT` / `VESSEL-IN`) — apply to every panel.

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

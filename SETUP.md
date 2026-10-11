# Setup

Getting from an empty machine to both dashboards running in Grafana, and
the loop for changing them afterwards. [README.md](README.md) covers what
the dashboards show and why they're built the way they are.

## What you need

- **Docker**, for Postgres (`docker-compose.yml`).
- **Grafana 12 or newer**, reachable from this machine (the examples assume
  `http://localhost:3000`). Running natively or in Docker both work.
- **Python 3.7+** with the packages in `requirements.txt` for the loader.
  The dashboard generators and `push_dashboard.py` use only the standard
  library, so any Python runs them.
- The weighbridge `.xlsx` exports, one per site. They're gitignored —
  they contain commercial data and never belong in the repo.

## 1. Start Postgres

```bash
docker compose up -d
```

Starts `vessel-postgres` on `localhost:5432` (database, user and password
all `vessel`), with data in the `vessel_pg_data` volume so it survives
restarts.

## 2. Install the loader's Python packages

```bash
pip install -r requirements.txt
```

Use whichever Python you'll run `load_data.py` with, and check it can see
the Postgres driver: `python -c "import psycopg2"`. If you have more than
one Python installed (e.g. Anaconda alongside python.org), they each need
their own install.

## 3. Load the ticket exports

```bash
python load_data.py /path/to/dublin.xlsx /path/to/foynes.xlsx /path/to/lisahally.xlsx /path/to/ringaskiddy.xlsx
```

Safe to re-run with the same or overlapping files: rows are upserted on
`(site, ticket)`. It also creates and seeds the `bay_capacity` table (next
step).

## 4. Fill in store names and capacities

Bay capacities and store names don't exist anywhere in the exports, so
the loader gives every discharge bay a placeholder: 5000t capacity and no
name (the dashboard shows the bay code instead). Set real values in
Postgres:

```bash
docker exec -it vessel-postgres psql -U vessel -d vessel
```

```sql
-- See what's there
SELECT * FROM bay_capacity ORDER BY site, bay;

-- Set one bay
UPDATE bay_capacity SET max_capacity = 4800, store_name = 'South Store 1'
WHERE site = 'RIN' AND bay = 'S1';
```

Re-running the loader never overwrites values you've set; it only adds
rows for bays it hasn't seen before.

## 5. Add the Postgres data source in Grafana

Connections → Add new connection → PostgreSQL:

| Field | Value |
|---|---|
| Host | `localhost:5432` |
| Database | `vessel` |
| User / Password | `vessel` / `vessel` |
| TLS/SSL mode | `disable` |

Then note the data source's **UID**: it's the last part of the URL on its
settings page (`.../connections/datasources/edit/<uid>`). Both generators
hardcode it as `DS = {...}` near the top of `generate_dashboard.py` and
`generate_live_dashboard.py` (currently `afxbuc9yby1ogd`). If yours is
different, change it in both files. README → Notes → "Datasource UID"
explains why it's hardcoded.

## 6. Create a Grafana service account token

`push_dashboard.py` deploys the dashboards through Grafana's HTTP API, so
it needs a token:

1. Administration → Users and access → Service accounts → **Add service
   account**. Give it a name (e.g. `dashboard-deploy`) and the **Editor**
   role. Editor is enough to query data sources and save dashboards;
   nothing here needs Admin.
2. Open the service account → **Add service account token** → Generate.
   Copy it (it starts `glsa_`); Grafana only shows it once.
3. Create your `.env` from the template and paste the token in:

   ```bash
   cp .env.example .env
   ```

   ```
   GRAFANA_URL=http://localhost:3000
   GRAFANA_TOKEN=glsa_...
   ```

`.env` is gitignored. Treat the token like a password: if it's ever been
pasted somewhere it shouldn't be (a chat, a ticket, a commit), delete it
on the service account page and generate a new one.

## 7. Check Grafana's dashboard layout feature

The live dashboard's per-voyage truck sections use Grafana's newer
dashboard layout (README → "Per-voyage truck sections" explains why).
Check it's on (the first line loads your `.env` into the shell):

```bash
set -a; . ./.env; set +a
curl -s -H "Authorization: Bearer $GRAFANA_TOKEN" http://localhost:3000/api/frontend/settings | grep -o '"dashboardNewLayouts":[a-z]*'
```

`"dashboardNewLayouts":true` means you're set. If it's missing or
`false`, enable it in `grafana.ini` and restart Grafana:

```ini
[feature_toggles]
enable = dashboardNewLayouts
```

Or, for Grafana in Docker, set the environment variable
`GF_FEATURE_TOGGLES_ENABLE=dashboardNewLayouts`.

Without it, everything else still works, but every voyage section lists
every active truck.

## 8. Generate and deploy the dashboards

```bash
python generate_dashboard.py
python push_dashboard.py despatch-and-demurrage.json
```

```bash
python generate_live_dashboard.py
python push_dashboard.py live-operations.json
```

Each `push_dashboard.py` run:

1. resolves every template variable against the live data source, the way
   Grafana would with every filter on "All";
2. runs every panel's SQL through Grafana (so `$__timeFrom()`-style
   macros expand exactly as they do in a panel) and prints `ok` / `EMPTY`
   / `FAIL` per panel;
3. **stops without uploading if any query fails**;
4. otherwise uploads the dashboard (overwriting the previous version;
   Grafana keeps the history) and reads it back;
5. for the live dashboard, moves the truck list onto the voyage section
   via Grafana's v2 dashboard API, so each section only lists its own
   trucks.

Open them at `http://localhost:3000/d/despatch-and-demurrage` and
`http://localhost:3000/d/live-operations`.

### Without a token

You can import the generated JSON by hand instead: Dashboards → New →
Import → upload `despatch-and-demurrage.json` or `live-operations.json`.
Nothing is validated first, and on the live dashboard every voyage section
lists every active truck (with "–" tiles for trucks not on it), because
step 5 above doesn't happen.

### Provisioning a containerised Grafana

`postgres.yml` (data source) and `dashboards.yml` (dashboard provider) are
Grafana provisioning files for a Grafana running in Docker. Mount them
into `/etc/grafana/provisioning/datasources/` and
`/etc/grafana/provisioning/dashboards/` respectively, and the generated
JSON files into `/etc/grafana/provisioning/dashboards/files/`. A
provisioned data source gets its own UID, so check step 5's UID note.
Provisioned dashboards skip `push_dashboard.py`, so the live dashboard
has the same limitation as a hand import.

## Changing a dashboard

Edit the generator (`generate_dashboard.py` or
`generate_live_dashboard.py`), never the generated JSON; then regenerate
and push as in step 8.

Useful `push_dashboard.py` options while iterating:

| Option | Use |
|---|---|
| `--check-only` | Validate every query without uploading. |
| `--from 2026-08-21T10:00:00Z --to 2026-08-22T12:00:00Z` | Validate against a past window instead of the dashboard's default range. The live board defaults to "today", which shows nothing if no voyage is running today. |
| `--var voyage=VOY.000567` | Pin a filter to one value instead of "All". Repeatable. |

`EMPTY` isn't a failure (a panel can legitimately have no rows for the
chosen window), but an unexpected one is worth a look at that window in
the browser.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `ModuleNotFoundError: No module named 'psycopg2'` | The Python running `load_data.py` doesn't have the requirements installed; see step 2. |
| `relation "bay_capacity" does not exist` | The loader hasn't run since `bay_capacity` was added. Re-run `load_data.py` on any export (it's safe). |
| `push_dashboard.py` fails with HTTP 401 | Token missing, mistyped or deleted; check `.env`. |
| HTTP 403 on upload | The service account's role is below Editor. |
| Every panel says "data source not found" | The data source UID differs from the one in the generators; see step 5. |
| Voyage dropdown shows "(no voyage in window)" | No voyage overlaps the picked time range. Widen it or move it back to when data exists. |
| Every voyage section lists every truck | The live dashboard was imported by hand or provisioned, or `dashboardNewLayouts` is off; see steps 7 and 8. |

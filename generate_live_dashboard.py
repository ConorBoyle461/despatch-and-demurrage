"""
Generates live-operations.json — a second, separate dashboard from
generate_dashboard.py, modeled directly on the "Ringaskiddy Discharge
Operations" status-board PDF the user shared. That board is a live,
single-voyage snapshot (per-truck today/vessel-total performance, a
per-bay store fill table, colour-coded thresholds) — a different kind of
tool from despatch-and-demurrage.json, which is the historical, filterable,
multi-voyage trend dashboard. Both stay in the repo; this one doesn't
replace the other.

Known gaps versus the PDF, and how they're handled (see README "Live
operations dashboard" section for the full reasoning):
  - The PDF's "Vessel" table is a per-HATCH breakdown of a vessel being
    loaded (OUT). No hatch number exists anywhere in the source exports,
    so this is downgraded to a per-VESSEL rollup (tons loaded), with no
    hatch split. Per user decision.
  - The PDF's "Store" table needs each bay's max capacity, which is a
    fixed physical constant that never appears in a weighbridge ticket.
    load_data.py seeds a bay_capacity reference table with a placeholder
    default (5000t) the first time each bay is seen; edit that table with
    real values directly in Postgres. Per user decision.
  - Vessel Name in the header is parsed from OUT tickets' location field
    ("<VESSEL NAME> - VOY.xxxxxx"). Most voyages in the initial load are
    pure discharge (VESSEL-IN only, no OUT tickets at all) and have no
    vessel name anywhere in the data — the header falls back to a plain
    note rather than guessing.

Point-in-time reconstruction: every cumulative/"vessel total" query is
bounded by $__timeTo() (the end of the picked time range), not real
wall-clock NOW() and not an unbounded whole-voyage scan. Set the time
range picker's end to a past moment and the whole board reconstructs
what it would have shown at that moment. Leave it at "now" (the default)
and it behaves like a live board. See README for a worked example.

Filters (all chained, all default to "All" so nothing is excluded until
you narrow one): site, voyage (single-select — this is a one-voyage-at-a-
time board), ticket_type, vehicle, bay. Not every filter touches every
panel — Store is inherently VESSEL-IN-only and Vessel is inherently
OUT-only regardless of the ticket_type picker, matching what those tables
already mean; bay only makes sense against VESSEL-IN activity (location
is a bay code only on those tickets), so it isn't applied to the Vessel
table.
"""
import json

DS = {"type": "postgres", "uid": "afxbuc9yby1ogd"}

def with_placeholder(query, label):
    """Make a variable query return a single placeholder value instead of
    nothing. An empty multi-select left on "All" expands to `IN ()`, which
    is a Postgres syntax error that breaks every chained variable and panel
    below it; IN ('<placeholder>') is valid SQL that matches no rows, and
    the dropdown says why it's empty. Option order comes from the variable's
    "sort" setting, since ORDER BY inside the subquery isn't preserved."""
    return (f"SELECT * FROM ({query}) v "
            f"UNION ALL SELECT '{label}' WHERE NOT EXISTS (SELECT 1 FROM ({query}) e)")


# Multi-select with an "All voyages" default — despite the module's
# original single-voyage framing, the board is still useful rolled up
# across every voyage currently in scope (e.g. "how's the whole site
# doing right now"), and narrowing back to one voyage is just picking it
# in the dropdown. Header panels (Site/Vessel Name/Commodity) aggregate
# across whatever's selected rather than assuming exactly one.
VOYAGE_EQ = "voyage IN (${voyage:sqlstring})"

# Applied to the Truck Performance table and the truck tip time series —
# the panels where every filter dimension is meaningful.
FULL_FILTER = (f"{VOYAGE_EQ} "
               "AND site IN (${site:sqlstring}) "
               "AND ticket_type IN (${ticket_type:sqlstring}) "
               "AND vehicle_num IN (${vehicle:sqlstring}) "
               "AND location IN (${bay:sqlstring})")

# Store/Vessel keep their hardcoded ticket_type (that's what makes them
# "Store" vs "Vessel"), but still honour site/vehicle so they agree with
# the rest of the board when you narrow those pickers. Store also honours
# bay; Vessel doesn't (location on OUT tickets is a vessel name, not a
# bay code, so filtering it by bay would just empty the table).
STORE_FILTER = (f"{VOYAGE_EQ} "
                "AND t.site IN (${site:sqlstring}) "
                "AND t.vehicle_num IN (${vehicle:sqlstring}) "
                "AND t.location IN (${bay:sqlstring})")
VESSEL_FILTER = (f"{VOYAGE_EQ} "
                 "AND site IN (${site:sqlstring}) "
                 "AND vehicle_num IN (${vehicle:sqlstring})")

# --- Thresholds, calibrated against the real combined dataset (7,056
# tickets, 178 truck-voyage groups with a usable activity span), NOT
# copied from the PDF's illustrative numbers, which are specific to that
# one mockup vessel/commodity and not a general target. See the
# conversation / commit history for the underlying percentile numbers:
#   tons/hr:  p10 4.7, p25 21.1, p50 49.1, p75 77.3, p90 114.0
#   tips/hr:  p10 0.13, p25 0.8, p50 1.76, p75 2.99, p90 3.67
#   inter-tip gap (min): p50 16.6, p75 22.8, p90 50.4, p95 77.4
TONS_PER_HR_RED_BELOW = 25      # ~p25 of real data — bottom quartile flagged
TIPS_PER_HR_RED_BELOW = 1.0     # ~p25 of real data
LAST_TIP_RED_ABOVE_MIN = 60     # sits between p90 (50min) and p95 (77min) of real inter-tip gaps

# Amber breakpoints for the 3-band (red/amber/green) per-truck tiles —
# the Truck Performance table's cell coloring stays 2-band (red/green,
# via low_is_bad_threshold/high_is_bad_threshold above), but the tiles
# use three_step_threshold with these, one step further into "worth
# watching but not yet a problem" territory. Reuses the same percentile
# basis: amber-to-green sits at the real median (p50), so "green" means
# at/above a typical truck's rate, not just "not terrible".
TONS_PER_HR_AMBER_TO_GREEN = 49   # p50 of real tons/hr
TIPS_PER_HR_AMBER_TO_GREEN = 1.76  # p50 of real tips/hr
LAST_TIP_AMBER_AT_MIN = 30        # same "worth a look" mark as the cycle-time outlier threshold elsewhere

# Store bay fill: watch from 80% full, flag at/over 100% (over the
# placeholder or real capacity).
BAY_FILL_AMBER_AT = 80
BAY_FILL_RED_AT = 100


def row_panel(id_, title, y, repeat=None):
    """A (non-collapsed) row header: owns every panel below it until the
    next row. With `repeat`, Grafana draws one copy of the row, and the
    panels in it, per value of that variable."""
    panel = {"id": id_, "type": "row", "title": title, "collapsed": False,
             "panels": [], "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}
    if repeat:
        panel["repeat"] = repeat
    return panel


def text_panel(id_, content, x, y, w=24, h=2):
    return {
        "id": id_,
        "type": "text",
        "title": "",
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {"mode": "markdown", "content": content},
    }


def stat_panel(id_, title, sql, x, y, w=6, h=3, unit="none"):
    return {
        "id": id_,
        "type": "stat",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "table", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "graphMode": "none",
                    "textMode": "value", "colorMode": "none"},
    }


def low_is_bad_threshold(green_at):
    """Red below `green_at`, green at/above it — for rate metrics where a
    LOW value is the problem (tons/hr, tips/hr)."""
    return {"mode": "absolute", "steps": [
        {"color": "red", "value": None},
        {"color": "green", "value": green_at},
    ]}


def high_is_bad_threshold(red_at):
    """Green below `red_at`, red at/above it — for metrics where a HIGH
    value is the problem (minutes since last tip, bay fill %)."""
    return {"mode": "absolute", "steps": [
        {"color": "green", "value": None},
        {"color": "red", "value": red_at},
    ]}


def three_step_threshold(amber_at, green_at, low_is_bad=True):
    """Red/amber/green with two breakpoints — used for the fill gauge
    (amber before red) and the median-ratio tiles (amber before green)."""
    if low_is_bad:
        return {"mode": "absolute", "steps": [
            {"color": "red", "value": None},
            {"color": "orange", "value": amber_at},
            {"color": "green", "value": green_at},
        ]}
    return {"mode": "absolute", "steps": [
        {"color": "green", "value": None},
        {"color": "orange", "value": amber_at},
        {"color": "red", "value": green_at},
    ]}


def truck_table_panel(id_, title, sql, x, y, w=24, h=10):
    return {
        "id": id_,
        "type": "table",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "table", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {
            "defaults": {},
            "overrides": [
                {"matcher": {"id": "byName", "options": "vehicle"},
                 "properties": [{"id": "displayName", "value": "Truck"}]},
                {"matcher": {"id": "byName", "options": "tons_today"},
                 "properties": [{"id": "displayName", "value": "Tons (today)"},
                                 {"id": "unit", "value": "short"}]},
                {"matcher": {"id": "byName", "options": "avg_tons_hr_today"},
                 "properties": [{"id": "displayName", "value": "Avg Tons/hr (today)"},
                                 {"id": "unit", "value": "short"},
                                 {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                 {"id": "thresholds", "value": low_is_bad_threshold(TONS_PER_HR_RED_BELOW)}]},
                {"matcher": {"id": "byName", "options": "tips_hr_today"},
                 "properties": [{"id": "displayName", "value": "Avg Tips/hr (today)"},
                                 {"id": "unit", "value": "short"},
                                 {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                 {"id": "thresholds", "value": low_is_bad_threshold(TIPS_PER_HR_RED_BELOW)}]},
                {"matcher": {"id": "byName", "options": "last_tip_min"},
                 "properties": [{"id": "displayName", "value": "Last Tip (mins)"},
                                 {"id": "unit", "value": "m"},
                                 {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                 {"id": "thresholds", "value": high_is_bad_threshold(LAST_TIP_RED_ABOVE_MIN)}]},
                {"matcher": {"id": "byName", "options": "tons_vessel"},
                 "properties": [{"id": "displayName", "value": "Tons (vessel total)"},
                                 {"id": "unit", "value": "short"}]},
                {"matcher": {"id": "byName", "options": "avg_tons_hr_vessel"},
                 "properties": [{"id": "displayName", "value": "Avg Tons/hr (vessel total)"},
                                 {"id": "unit", "value": "short"},
                                 {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                                 {"id": "thresholds", "value": low_is_bad_threshold(TONS_PER_HR_RED_BELOW)}]},
            ],
        },
    }


def plain_table_panel(id_, title, sql, x, y, w=12, h=8, description=""):
    panel = {
        "id": id_,
        "type": "table",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "table", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {}, "overrides": []},
    }
    if description:
        panel["description"] = description
    return panel


def header_value_panel(id_, title, sql, x, y, w=6, h=3):
    """Single-value header tiles (Site / Vessel Name / Commodity).
    Deliberately a `table` panel, not a `stat` panel: these return a
    single TEXT value (string_agg / regexp_replace), and in this Grafana
    setup stat panels showed "No Data" for text-only results even though
    the same query works fine as a table (confirmed against the Truck
    Performance table, which mixes text and numeric columns and renders
    correctly) — every stat panel elsewhere in this dashboard aggregates
    to a plain number, which is the difference. Header hidden and a
    borderless look configured so it still reads as a tile, not a grid.
    """
    return {
        "id": id_,
        "type": "table",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "table", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"showHeader": False, "cellHeight": "lg"},
    }


def timeseries_panel(id_, title, sql, x, y, w=24, h=8, unit="short", description=""):
    panel = {
        "id": id_,
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "time_series", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {
            "unit": unit,
            "custom": {"drawStyle": "line", "lineWidth": 1, "pointSize": 5,
                       "showPoints": "always", "spanNulls": False},
        }, "overrides": []},
    }
    if description:
        panel["description"] = description
    return panel


def barchart_panel(id_, title, sql, x, y, w=12, h=6, unit="short", description=""):
    panel = {
        "id": id_,
        "type": "barchart",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "table", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"xField": "metric"},
    }
    if description:
        panel["description"] = description
    return panel


def bargauge_panel(id_, title, sql, x, y, w=12, h=6, unit="percent",
                    thresholds=None, min_=0, max_=None, description=""):
    panel = {
        "id": id_,
        "type": "bargauge",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "table", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {"unit": unit, "min": min_}, "overrides": []},
        # values: True draws one bar per row (labelled by the row's text
        # column) instead of reducing the whole column to a single bar.
        "options": {"displayMode": "gradient", "orientation": "horizontal",
                    "showUnfilled": True, "namePlacement": "left",
                    "reduceOptions": {"values": True, "calcs": [], "limit": 100}},
    }
    if max_ is not None:
        panel["fieldConfig"]["defaults"]["max"] = max_
    if thresholds:
        panel["fieldConfig"]["defaults"]["thresholds"] = thresholds
    if description:
        panel["description"] = description
    return panel


# Shared WHERE clause for the per-truck panels: every filter, plus the one
# truck this repeated copy is for, limited to the picked time window.
TRUCK_WHERE = """site IN (${site:sqlstring})
          AND voyage IN (${voyage:sqlstring})
          AND ticket_type IN (${ticket_type:sqlstring})
          AND location IN (${bay:sqlstring})
          AND vehicle_num = '$vehicle_active'
          AND $__timeFilter(date_time2)"""

# (column, display name, unit, decimals, thresholds) for the four stats on
# each truck's summary panel. Tons Today has no natural good/bad band so
# it's a flat colour; the other three use a 3-band (red/amber/green)
# version of the Truck Performance table's thresholds.
TRUCK_STATS = [
    ("tons_today", "Tons Today", "short", 1, None),
    ("tons_hr", "Avg Tons/hr", "short", 1,
     three_step_threshold(TONS_PER_HR_RED_BELOW, TONS_PER_HR_AMBER_TO_GREEN, low_is_bad=True)),
    ("tips_hr", "Avg Tips/hr", "short", 2,
     three_step_threshold(TIPS_PER_HR_RED_BELOW, TIPS_PER_HR_AMBER_TO_GREEN, low_is_bad=True)),
    ("last_tip_min", "Last Tip", "m", 0,
     three_step_threshold(LAST_TIP_AMBER_AT_MIN, LAST_TIP_RED_ABOVE_MIN, low_is_bad=False)),
]
TONS_TODAY_THRESHOLD = {"mode": "absolute", "steps": [{"color": "blue", "value": None}]}


def truck_summary_panel(id_, x, y, w=12, h=5):
    """One panel per truck, repeated over the hidden $vehicle_active
    variable (only trucks with a ticket in the picked window get one). A
    single query returns all four stats as columns; a stat panel shows
    each column as its own background-coloured tile, so the truck's four
    numbers sit together under one title instead of four loose panels.
    """
    sql = f"""SELECT
            COALESCE(ROUND(SUM(net_weight)::numeric, 1), 0) AS tons_today,
            ROUND((SUM(net_weight) / NULLIF(EXTRACT(EPOCH FROM (MAX(date_time2) - MIN(date_time2)))/3600.0, 0))::numeric, 1) AS tons_hr,
            ROUND((COUNT(*) / NULLIF(EXTRACT(EPOCH FROM (MAX(date_time2) - MIN(date_time2)))/3600.0, 0))::numeric, 2) AS tips_hr,
            ROUND((EXTRACT(EPOCH FROM ($__timeTo() - MAX(date_time2)))/60.0)::numeric, 0) AS last_tip_min
        FROM tickets
        WHERE {TRUCK_WHERE}"""
    overrides = [
        {"matcher": {"id": "byName", "options": col},
         "properties": [{"id": "displayName", "value": name},
                        {"id": "unit", "value": unit},
                        {"id": "decimals", "value": decimals},
                        {"id": "thresholds", "value": thr or TONS_TODAY_THRESHOLD}]}
        for col, name, unit, decimals, thr in TRUCK_STATS
    ]
    return {
        "id": id_,
        "type": "stat",
        "title": "$vehicle_active",
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "table", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {"noValue": "–"}, "overrides": overrides},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": ""},
                    "orientation": "vertical", "textMode": "value_and_name",
                    "colorMode": "background", "graphMode": "none",
                    "justifyMode": "center", "wideLayout": True},
        "repeat": "vehicle_active",
        "repeatDirection": "v",
    }


def truck_timeseries_panel(id_, x, y, w=12, h=5):
    """The same truck's activity through the picked window, beside its
    summary panel: each tip's net weight as a bar, and the running total
    as a line on the right-hand axis. Repeats over $vehicle_active with
    the same y/h as truck_summary_panel, so Grafana lays each truck's
    pair out on one line."""
    sql = f"""SELECT date_time2 AS time,
               net_weight AS tip,
               SUM(net_weight) OVER (ORDER BY date_time2) AS cumulative
        FROM tickets
        WHERE {TRUCK_WHERE}
        ORDER BY date_time2"""
    return {
        "id": id_,
        "type": "timeseries",
        "title": "$vehicle_active — tips & running total (t)",
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "table", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {"unit": "short", "decimals": 1, "min": 0,
                                     "custom": {"lineWidth": 2, "fillOpacity": 10,
                                                "showPoints": "never", "spanNulls": True}},
                        "overrides": [
            {"matcher": {"id": "byName", "options": "tip"},
             "properties": [{"id": "displayName", "value": "Tip (t)"},
                            {"id": "custom.drawStyle", "value": "bars"},
                            {"id": "custom.fillOpacity", "value": 80},
                            {"id": "custom.barWidthFactor", "value": 0.3},
                            {"id": "color", "value": {"mode": "fixed", "fixedColor": "blue"}}]},
            {"matcher": {"id": "byName", "options": "cumulative"},
             "properties": [{"id": "displayName", "value": "Running total (t)"},
                            {"id": "custom.axisPlacement", "value": "right"},
                            {"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]},
        ]},
        "options": {"legend": {"displayMode": "list", "placement": "bottom"},
                    "tooltip": {"mode": "multi"}},
        "repeat": "vehicle_active",
        "repeatDirection": "v",
    }


panels = []
pid = 1

panels.append(text_panel(pid,
    "# Live Discharge / Dispatch Operations — Voyage(s) $voyage",
    x=0, y=0, w=24, h=2)); pid += 1

panels.append(header_value_panel(pid, "Site",
    f"""SELECT string_agg(DISTINCT site, ', ' ORDER BY site) AS site
        FROM tickets WHERE {VOYAGE_EQ}""",
    x=0, y=2, w=4, h=3)); pid += 1

panels.append(header_value_panel(pid, "Vessel Name",
    f"""SELECT COALESCE(
            (SELECT string_agg(DISTINCT regexp_replace(location, '\\s*-\\s*VOY\\..*$', ''), ', ' ORDER BY regexp_replace(location, '\\s*-\\s*VOY\\..*$', ''))
             FROM tickets WHERE {VOYAGE_EQ} AND ticket_type = 'OUT'),
            '(no OUT tickets on the selected voyage(s) — vessel name unavailable)'
        ) AS vessel_name""",
    x=4, y=2, w=12, h=3)); pid += 1

panels.append(header_value_panel(pid, "Commodity",
    f"""SELECT string_agg(DISTINCT item_number, ', ' ORDER BY item_number) AS commodity
        FROM tickets WHERE {VOYAGE_EQ}""",
    x=16, y=2, w=8, h=3)); pid += 1

# --- Per-voyage sections of per-truck rows ---
# A row repeated per voyage, each holding one summary panel (4 coloured
# stats) + time series pair per truck active on THAT voyage in the picked
# window; see truck_summary_panel and truck_timeseries_panel.
#
# Grafana's classic repeats can't nest: a panel repeat inside a repeated
# row sees the dashboard-wide $vehicle_active list, so every voyage would
# list every truck. Grafana's newer (v2) layout lets a row carry its own
# variables, which are re-queried per repeated copy with that copy's
# $voyage, so ROW_VARIABLES below asks push_dashboard.py to move
# $vehicle_active onto the voyage row after uploading. Without that step
# (e.g. a hand import of this JSON) the board still works, but each
# voyage section lists every active truck, with "–" for ones not on it.
panels.append(row_panel(pid, "Voyage $voyage", y=5, repeat="voyage")); pid += 1
panels.append(truck_summary_panel(pid, x=0, y=6)); pid += 1
panels.append(truck_timeseries_panel(pid, x=12, y=6)); pid += 1
panels.append(row_panel(pid, "Trucks, stores & vessels", y=11)); pid += 1

# --- Per-truck live performance table ---
# `overall` is the driving table so every truck that has worked this
# voyage UP TO the picked point in time appears, even ones with zero
# activity in the "today" window (tons_today then reads 0 via COALESCE
# rather than the truck disappearing from the list).
#
# `today` is scoped by the dashboard's own time-range picker
# ($__timeFilter) — set it to any single day (past or present) to see
# that day's activity.
#
# `overall` and `last_tip_min` are bounded by $__timeTo() (the END of
# whatever range is picked) for point-in-time reconstruction — see the
# module docstring.
#
# Both CTEs also honour the site / ticket_type / vehicle / bay filters
# (FULL_FILTER), so narrowing any of those pickers narrows this table too.
panels.append(truck_table_panel(pid, "Truck Performance",
    f"""WITH today AS (
            SELECT vehicle_num,
                   SUM(net_weight) AS tons_today,
                   COUNT(*) AS tips_today,
                   MIN(date_time2) AS tmin_today,
                   MAX(date_time2) AS tmax_today
            FROM tickets
            WHERE {FULL_FILTER} AND $__timeFilter(date_time2)
            GROUP BY vehicle_num
        ),
        overall AS (
            SELECT vehicle_num,
                   SUM(net_weight) AS tons_vessel,
                   MIN(date_time2) AS tmin_all,
                   MAX(date_time2) AS tmax_all
            FROM tickets
            WHERE {FULL_FILTER} AND date_time2 <= $__timeTo()
            GROUP BY vehicle_num
        )
        SELECT
            o.vehicle_num AS vehicle,
            COALESCE(ROUND(t.tons_today::numeric, 1), 0) AS tons_today,
            ROUND((t.tons_today / NULLIF(EXTRACT(EPOCH FROM (t.tmax_today - t.tmin_today))/3600.0, 0))::numeric, 1) AS avg_tons_hr_today,
            ROUND((t.tips_today / NULLIF(EXTRACT(EPOCH FROM (t.tmax_today - t.tmin_today))/3600.0, 0))::numeric, 2) AS tips_hr_today,
            ROUND((EXTRACT(EPOCH FROM ($__timeTo() - o.tmax_all))/60.0)::numeric, 1) AS last_tip_min,
            ROUND(o.tons_vessel::numeric, 1) AS tons_vessel,
            ROUND((o.tons_vessel / NULLIF(EXTRACT(EPOCH FROM (o.tmax_all - o.tmin_all))/3600.0, 0))::numeric, 1) AS avg_tons_hr_vessel
        FROM overall o
        LEFT JOIN today t ON t.vehicle_num = o.vehicle_num
        ORDER BY o.tons_vessel DESC""",
    x=0, y=12, w=24, h=10)); pid += 1

# --- Truck tip time series ---
# The original ask this whole dashboard traces back to: each truck's
# individual tips plotted through the day, not just summarised in the
# table above. One series per vehicle_num — narrow the vehicle filter
# above if this gets busy (a full site's worth of trucks is a lot of
# series at once, same caveat as the historical dashboard's per-truck
# timeline panel).
panels.append(timeseries_panel(pid, "Truck Tips Through Time (net weight per tip)",
    f"""SELECT date_time2 AS time, vehicle_num AS metric, net_weight AS value
        FROM tickets
        WHERE {FULL_FILTER} AND $__timeFilter(date_time2)
        ORDER BY date_time2""",
    x=0, y=22, w=24, h=8, unit="short",
    description="One series per truck (vehicle filter above). Narrow it if this gets crowded.")); pid += 1

# --- Store table: per-bay fill level (VESSEL-IN / discharge only) ---
# Scoped to this voyage's own contribution to each bay, not the bay's
# true current fill level — we only see inbound truck movements, not
# prior stock already in the bay or later offtake, so this answers "how
# much of THIS voyage has gone into this bay so far", matching the PDF's
# single-voyage framing. max_capacity/space come from the bay_capacity
# table load_data.py seeds with a 5000t placeholder default per bay —
# edit that table directly in Postgres with real values; Space can (and,
# with the placeholder still in effect, likely will) come out negative
# where a bay's real capacity is below 5000t. That's the placeholder
# telling you it needs a real number, not a bug.
# Bounded by $__timeTo() for point-in-time reconstruction; honours
# site/vehicle/bay filters (not ticket_type — this table IS the
# VESSEL-IN view, so that filter dimension doesn't apply here).
# store_name falls back to the bay code until it's filled in on
# bay_capacity (the exports only carry the code).
STORE_NAME = "COALESCE(MAX(bc.store_name), t.location)"
STORE_FROM = f"""FROM tickets t
        LEFT JOIN bay_capacity bc ON bc.site = t.site AND bc.bay = t.location
        WHERE {STORE_FILTER} AND t.ticket_type = 'VESSEL-IN' AND t.date_time2 <= $__timeTo()
        GROUP BY t.site, t.location"""
STORE_SQL = f"""SELECT
            {STORE_NAME} AS store,
            ROUND(SUM(t.net_weight)::numeric, 1) AS used,
            MAX(bc.max_capacity) AS max_capacity,
            ROUND((SUM(t.net_weight) / NULLIF(MAX(bc.max_capacity), 0) * 100)::numeric, 1) AS fill_pct
        {STORE_FROM}
        ORDER BY store"""
store_table = plain_table_panel(pid, "Store (by bay) — this voyage's discharge",
    STORE_SQL,
    x=0, y=30, w=12, h=8,
    description="Empty when this voyage has no VESSEL-IN (discharge) tickets — i.e. it's a pure dispatch/loading voyage. Store names and capacities come from the bay_capacity table — see README.")
store_table["fieldConfig"]["overrides"] = [
    {"matcher": {"id": "byName", "options": "store"},
     "properties": [{"id": "displayName", "value": "Store"}]},
    {"matcher": {"id": "byName", "options": "used"},
     "properties": [{"id": "displayName", "value": "Storage used (t)"}]},
    {"matcher": {"id": "byName", "options": "max_capacity"},
     "properties": [{"id": "displayName", "value": "Max capacity (t)"}]},
    {"matcher": {"id": "byName", "options": "fill_pct"},
     "properties": [{"id": "displayName", "value": "Fill"},
                    {"id": "unit", "value": "percent"},
                    {"id": "min", "value": 0}, {"id": "max", "value": 100},
                    {"id": "thresholds", "value": three_step_threshold(BAY_FILL_AMBER_AT, BAY_FILL_RED_AT, low_is_bad=False)},
                    {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}}]},
]
panels.append(store_table); pid += 1

# --- Vessel table: per-vessel rollup for OUT (dispatch/loading) tickets ---
# Downgraded from the PDF's per-HATCH breakdown to per-VESSEL, since no
# hatch number exists anywhere in the source data (see module docstring).
# Empty for the majority of voyages in the initial load, which are pure
# discharge (VESSEL-IN only) with zero OUT tickets — that's expected, not
# a bug: it means no vessel was being loaded on this voyage.
# Bounded by $__timeTo(); honours site/vehicle filters (not bay — a
# vessel's location field is a vessel name, not a bay code).
VESSEL_SQL = f"""SELECT
            COALESCE(regexp_replace(location, '\\s*-\\s*VOY\\..*$', ''), location) AS vessel,
            ROUND(SUM(net_weight)::numeric, 1) AS tons_loaded,
            COUNT(*) AS tips
        FROM tickets
        WHERE {VESSEL_FILTER} AND ticket_type = 'OUT' AND date_time2 <= $__timeTo()
        GROUP BY vessel
        ORDER BY tons_loaded DESC"""
panels.append(plain_table_panel(pid, "Vessel (loading) — no hatch-level data available",
    VESSEL_SQL,
    x=12, y=30, w=12, h=8,
    description="Empty when this voyage has no OUT (dispatch) tickets — i.e. it's a pure discharge voyage. No hatch-level split: the source data has no hatch field.")); pid += 1

# --- Store bay fill, visualised as a bar gauge (percent of capacity) ---
# Same underlying numbers as the Store table above, reshaped to a
# fill-percentage so it reads as a gauge rather than a row of figures.
# Same caveat as the table: capacity is the 5000t placeholder until you
# edit bay_capacity with real figures, so a bar reading exactly 100%
# green-to-red is telling you the placeholder, not necessarily reality.
panels.append(bargauge_panel(pid, "Store Bay Fill (% of capacity)",
    # Bar label carries the store name plus used / max tonnage, so each
    # bar reads on its own without cross-referencing the table.
    f"""SELECT
            {STORE_NAME} || '  ·  ' || ROUND(SUM(t.net_weight)::numeric, 0)
                || ' / ' || ROUND(MAX(bc.max_capacity)::numeric, 0) || ' t' AS store,
            ROUND((SUM(t.net_weight) / NULLIF(MAX(bc.max_capacity), 0) * 100)::numeric, 1) AS fill_pct
        {STORE_FROM}
        ORDER BY MIN(t.location)""",
    x=0, y=38, w=12, h=6, unit="percent", min_=0, max_=120,
    thresholds=three_step_threshold(BAY_FILL_AMBER_AT, BAY_FILL_RED_AT, low_is_bad=False),
    description="Same figures as the Store table, as a fill gauge. Capacity is the bay_capacity placeholder until corrected — see README.")); pid += 1

# --- Vessel loading, visualised as a bar chart ---
panels.append(barchart_panel(pid, "Vessel Loading (tons)",
    VESSEL_SQL,
    x=12, y=38, w=12, h=6, unit="short",
    description="Same figures as the Vessel table, as a bar chart. Empty on pure-discharge voyages.")); pid += 1


dashboard = {
    "title": "Live Operations",
    "uid": "live-operations",
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "time": {"from": "now/d", "to": "now"},
    "timezone": "browser",
    "refresh": "1m",
    "panels": panels,
    # Not a Grafana field: read (and stripped) by push_dashboard.py, which
    # moves these variables onto the row repeated over the key variable.
    "x-row-variables": {"voyage": ["vehicle_active"]},
    "templating": {
        "list": [
            {
                "name": "site",
                "type": "query",
                "datasource": DS,
                "query": "SELECT DISTINCT site FROM tickets ORDER BY 1",
                "refresh": 1,
                "multi": True,
                "includeAll": True,
                "current": {"text": "All", "value": "$__all"},
            },
            {
                "name": "voyage",
                "type": "query",
                "datasource": DS,
                # Multi-select, defaulting to "All" — see VOYAGE_EQ comment.
                # Chained on $site, and limited to voyages that were in
                # progress at some point during the picked time range: a
                # voyage's span runs from its first ticket opening to its
                # last ticket closing, and it's listed if that span overlaps
                # the window at all (started before the window ends AND
                # finished after it starts). LEAST/GREATEST cover the
                # handful of tickets whose Date+Time2 precedes Date+Time.
                # "refresh": 2 (on time range change) so the list follows
                # the time picker, not just dashboard load.
                "query": ("SELECT voyage FROM tickets "
                          "WHERE site IN (${site:sqlstring}) AND voyage IS NOT NULL "
                          "GROUP BY voyage "
                          "HAVING MIN(LEAST(date_time, date_time2)) <= $__timeTo() "
                          "AND MAX(GREATEST(date_time, date_time2)) >= $__timeFrom() "
                          "ORDER BY 1 DESC"),
                "refresh": 2,
                "multi": True,
                "includeAll": True,
                "current": {"text": "All", "value": "$__all"},
            },
            {
                "name": "ticket_type",
                "type": "query",
                "datasource": DS,
                "query": "SELECT DISTINCT ticket_type FROM tickets ORDER BY 1",
                "refresh": 1,
                "multi": True,
                "includeAll": True,
                "current": {"text": "All", "value": "$__all"},
            },
            {
                "name": "bay",
                "type": "query",
                "datasource": DS,
                # Chained on site/voyage. Only VESSEL-IN tickets carry a
                # bay code in `location`, so this lists bays, not vessels.
                "query": ("SELECT DISTINCT location FROM tickets "
                          "WHERE site IN (${site:sqlstring}) "
                          "AND voyage IN (${voyage:sqlstring}) "
                          "AND ticket_type = 'VESSEL-IN' "
                          "ORDER BY 1"),
                "refresh": 1,
                "multi": True,
                "includeAll": True,
                "current": {"text": "All", "value": "$__all"},
            },
            {
                "name": "vehicle",
                "type": "query",
                "datasource": DS,
                # Chained on site/voyage/ticket_type/bay, so the picker
                # only ever lists vehicles that actually appear under the
                # current filter. Also drives the repeated median-ratio
                # tiles at the bottom of the board (one tile per value
                # here when set to "All").
                "query": ("SELECT DISTINCT vehicle_num FROM tickets "
                          "WHERE site IN (${site:sqlstring}) "
                          "AND voyage IN (${voyage:sqlstring}) "
                          "AND ticket_type IN (${ticket_type:sqlstring}) "
                          "AND location IN (${bay:sqlstring}) "
                          "ORDER BY 1"),
                "refresh": 1,
                "multi": True,
                "includeAll": True,
                "current": {"text": "All", "value": "$__all"},
            },
            {
                "name": "vehicle_active",
                "type": "query",
                "datasource": DS,
                # Hidden — not a user-facing filter, purely the repeat
                # source for the per-truck stat groups above the Truck
                # Performance table. Same chaining as $vehicle, plus
                # $__timeFilter so only trucks with at least one ticket in
                # the CURRENTLY PICKED time window get a group — a truck
                # with no activity in that window simply gets no panel,
                # per your "only show tiles where there's data in the time
                # window" ask, rather than an empty one. "refresh": 2 (on
                # time range change) is deliberate here, unlike the other
                # variables — this one's list genuinely depends on the
                # time range, so it needs to re-query when that changes.
                "query": ("SELECT DISTINCT vehicle_num FROM tickets "
                          "WHERE site IN (${site:sqlstring}) "
                          "AND voyage IN (${voyage:sqlstring}) "
                          "AND ticket_type IN (${ticket_type:sqlstring}) "
                          "AND location IN (${bay:sqlstring}) "
                          "AND vehicle_num IN (${vehicle:sqlstring}) "
                          "AND $__timeFilter(date_time2) "
                          "ORDER BY 1"),
                "refresh": 2,
                "multi": True,
                "includeAll": True,
                "current": {"text": "All", "value": "$__all"},
                "hide": 2,
            },
        ]
    },
}

# Variables that can come back empty (no voyage active in the picked
# window, a loading-only voyage with no bays, ...) get a placeholder value;
# see with_placeholder. sort: 1 = alphabetical asc, 2 = desc.
PLACEHOLDERS = {
    "voyage": ("(no voyage in window)", 2),
    "bay": ("(no bays)", 1),
    "vehicle": ("(no vehicles)", 1),
    "vehicle_active": ("(no active trucks)", 1),
}
for v in dashboard["templating"]["list"]:
    if v["name"] in PLACEHOLDERS:
        label, sort = PLACEHOLDERS[v["name"]]
        v["query"] = with_placeholder(v["query"], label)
        v["sort"] = sort

with open("live-operations.json", "w") as f:
    json.dump(dashboard, f, indent=2)

print("Wrote live-operations.json")
print(f"Total panels: {len(panels)}")

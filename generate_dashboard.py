"""
Generates grafana/dashboards/despatch-and-demurrage.json

Kept as a script (rather than hand-edited JSON) so the panel/query design
is versioned and reviewable as code, not as a diff of a giant blob.

Rebuilt for the real multi-site dataset (Dublin, Foynes, Lisahally,
Ringaskiddy, ...), which is materially different from the single-voyage
VesselSample.xlsx this dashboard originally shipped against:
  - Multiple sites, multiple voyages per site (22 voyages across 4 sites
    in the initial load) -> site and voyage are both filter variables.
  - Two opposite cargo flows per site (OUT = dispatch, loading a vessel;
    VESSEL-IN = demurrage, unloading one) -> ticket_type is a filter
    variable too, and KPIs report both flows separately rather than
    summing them into a number that means nothing.
  - Truck cardinality is much higher (17-113 distinct vehicles per site)
    -> per-truck panels are Top-N (by crossing count within the current
    filter), not "one bar per truck", which would be unreadable past a
    handful of vehicles.
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


FILTER = ("site IN (${site:sqlstring}) "
          "AND voyage IN (${voyage:sqlstring}) "
          "AND ticket_type IN (${ticket_type:sqlstring})")

# How many trucks to show in any per-truck panel. Dublin alone has 113
# distinct vehicles in the initial load; a bar/series per truck past this
# is unreadable, so per-truck panels rank by crossing count (within the
# current filter) and keep only the busiest TOP_N.
TOP_N = 15

TOP_N_TRUCKS_SUBQUERY = f"""(
    SELECT vehicle_num FROM tickets
    WHERE {FILTER}
    GROUP BY vehicle_num
    ORDER BY COUNT(*) DESC
    LIMIT {TOP_N}
)"""


def stat_panel(id_, title, sql, x, y, w=6, h=4, unit="short", decimals=1):
    return {
        "id": id_,
        "type": "stat",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "table", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals}, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]}, "graphMode": "none"},
    }


def timeseries_panel(id_, title, sql, x, y, w=12, h=8, unit="short", thresholds=None):
    custom = {
        "drawStyle": "line",
        "lineWidth": 2,
        "pointSize": 5,
        "showPoints": "auto",
        "spanNulls": False,
    }
    panel = {
        "id": id_,
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "time_series", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {"unit": unit, "custom": custom}, "overrides": []},
    }
    if thresholds:
        panel["fieldConfig"]["defaults"]["thresholds"] = thresholds
        custom["thresholdsStyle"] = {"mode": "line"}
    return panel


def barchart_panel(id_, title, sql, x, y, w=8, h=8, unit="short"):
    return {
        "id": id_,
        "type": "barchart",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "table", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {"xField": "metric"},
    }


def state_timeline_panel(id_, title, sql, x, y, w=24, h=8):
    return {
        "id": id_,
        "type": "state-timeline",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "time_series", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"mergeValues": True, "showValue": "never"},
    }


def table_panel(id_, title, sql, x, y, w=24, h=8):
    return {
        "id": id_,
        "type": "table",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"datasource": DS, "format": "table", "rawSql": sql, "refId": "A"}],
        "fieldConfig": {"defaults": {}, "overrides": []},
    }


def dual_axis_timeseries_panel(id_, title, sql_left, sql_right, right_match_regex,
                                x, y, w=24, h=8, unit_left="short", unit_right="short"):
    """
    Two queries in one time series panel, second one pushed to a right-hand
    axis via a field override matched by regex on the series name. Used for
    comparing two differently-scaled metrics (e.g. net weight in tonnes vs
    cycle time in minutes) per tick, split into per-truck series by name.
    """
    return {
        "id": id_,
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [
            {"datasource": DS, "format": "time_series", "rawSql": sql_left, "refId": "A"},
            {"datasource": DS, "format": "time_series", "rawSql": sql_right, "refId": "B"},
        ],
        "fieldConfig": {
            "defaults": {
                "unit": unit_left,
                "custom": {"drawStyle": "line", "lineWidth": 2, "pointSize": 5, "showPoints": "auto"},
            },
            "overrides": [
                {
                    "matcher": {"id": "byRegexp", "options": right_match_regex},
                    "properties": [
                        {"id": "custom.axisPlacement", "value": "right"},
                        {"id": "unit", "value": unit_right},
                    ],
                }
            ],
        },
    }


panels = []
pid = 1

# --- Row: KPIs ---
panels.append(stat_panel(pid, "Total Net Weight (t)",
    f"SELECT SUM(net_weight) FROM tickets WHERE {FILTER}",
    x=0, y=0, w=4, h=4, unit="short", decimals=2)); pid += 1

panels.append(stat_panel(pid, "Dispatch (OUT) Net Weight (t)",
    f"SELECT COALESCE(SUM(net_weight),0) FROM tickets WHERE {FILTER} AND ticket_type = 'OUT'",
    x=4, y=0, w=4, h=4, unit="short", decimals=2)); pid += 1

panels.append(stat_panel(pid, "Demurrage (VESSEL-IN) Net Weight (t)",
    f"SELECT COALESCE(SUM(net_weight),0) FROM tickets WHERE {FILTER} AND ticket_type = 'VESSEL-IN'",
    x=8, y=0, w=4, h=4, unit="short", decimals=2)); pid += 1

panels.append(stat_panel(pid, "Total Weighbridge Crossings",
    f"SELECT COUNT(*) FROM tickets WHERE {FILTER}",
    x=12, y=0, w=4, h=4, unit="none", decimals=0)); pid += 1

panels.append(stat_panel(pid, "Active Trucks",
    f"SELECT COUNT(DISTINCT vehicle_num) FROM tickets WHERE {FILTER}",
    x=16, y=0, w=4, h=4, unit="none", decimals=0)); pid += 1

panels.append(stat_panel(pid, "Avg Cycle Time (min)",
    f"SELECT AVG(cycle_seconds)/60.0 FROM tickets WHERE {FILTER}",
    x=20, y=0, w=4, h=4, unit="short", decimals=1)); pid += 1

# --- Row: Throughput over time ---
# Colored by ticket_type (metric column) so the two opposite cargo flows
# read as distinct series rather than blending into one line.
panels.append(timeseries_panel(pid, "Net Weight per Crossing Over Time",
    f"""SELECT date_time2 AS time, ticket_type AS metric, net_weight AS value
        FROM tickets
        WHERE {FILTER} AND $__timeFilter(date_time2)
        ORDER BY date_time2""",
    x=0, y=4, w=12, h=8, unit="short")); pid += 1

panels.append(timeseries_panel(pid, "Cumulative Net Weight",
    f"""SELECT date_time2 AS time, ticket_type AS metric,
               SUM(net_weight) OVER (PARTITION BY ticket_type ORDER BY date_time2) AS value
        FROM tickets
        WHERE {FILTER} AND $__timeFilter(date_time2)
        ORDER BY date_time2""",
    x=12, y=4, w=12, h=8, unit="short")); pid += 1

# --- Row: Per-site breakdown ---
panels.append(barchart_panel(pid, "Net Weight by Site (t)",
    f"""SELECT site AS metric, SUM(net_weight) AS value
        FROM tickets WHERE {FILTER}
        GROUP BY site ORDER BY value DESC""",
    x=0, y=12, w=8, h=8)); pid += 1

panels.append(barchart_panel(pid, "Crossings by Site",
    f"""SELECT site AS metric, COUNT(*) AS value
        FROM tickets WHERE {FILTER}
        GROUP BY site ORDER BY value DESC""",
    x=8, y=12, w=8, h=8, unit="none")); pid += 1

panels.append(barchart_panel(pid, "Avg Cycle Time by Site (min)",
    f"""SELECT site AS metric, AVG(cycle_seconds)/60.0 AS value
        FROM tickets WHERE {FILTER}
        GROUP BY site ORDER BY value DESC""",
    x=16, y=12, w=8, h=8)); pid += 1

# --- Row: Per-truck breakdown (Top N busiest trucks within current filter) ---
panels.append(barchart_panel(pid, f"Net Weight by Truck (t) — Top {TOP_N}",
    f"""SELECT vehicle_num AS metric, SUM(net_weight) AS value
        FROM tickets WHERE {FILTER} AND vehicle_num IN {TOP_N_TRUCKS_SUBQUERY}
        GROUP BY vehicle_num ORDER BY value DESC""",
    x=0, y=20, w=6, h=8)); pid += 1

panels.append(barchart_panel(pid, f"Crossings by Truck — Top {TOP_N}",
    f"""SELECT vehicle_num AS metric, COUNT(*) AS value
        FROM tickets WHERE {FILTER} AND vehicle_num IN {TOP_N_TRUCKS_SUBQUERY}
        GROUP BY vehicle_num ORDER BY value DESC""",
    x=6, y=20, w=6, h=8, unit="none")); pid += 1

panels.append(barchart_panel(pid, f"Avg Cycle Time by Truck (min) — Top {TOP_N}",
    f"""SELECT vehicle_num AS metric, AVG(cycle_seconds)/60.0 AS value
        FROM tickets WHERE {FILTER} AND vehicle_num IN {TOP_N_TRUCKS_SUBQUERY}
        GROUP BY vehicle_num ORDER BY value DESC""",
    x=12, y=20, w=6, h=8)); pid += 1

panels.append(barchart_panel(pid, f"Avg Net Weight per Tip by Truck (t) — Top {TOP_N}",
    f"""SELECT vehicle_num AS metric, AVG(net_weight) AS value
        FROM tickets WHERE {FILTER} AND vehicle_num IN {TOP_N_TRUCKS_SUBQUERY}
        GROUP BY vehicle_num ORDER BY value DESC""",
    x=18, y=20, w=6, h=8)); pid += 1

# --- Row: Cycle time / demurrage detail ---
# 30-minute threshold recalibrated against the full combined dataset
# (7,056 rows across 4 sites), not just the original single-voyage sample:
# median cycle time ~7.1 min, p75 ~9.3 min, p90 ~13.9 min, p95 ~19.7 min,
# p97 ~26 min. 30 min flags 179/7056 rows (~2.5%) — consistent in spirit
# with the original sample-based threshold (~7% of a much smaller, noisier
# set) and still a genuine long tail rather than a broad swath of normal
# tickets. Re-check this periodically as more data accumulates.
panels.append(timeseries_panel(pid, "Cycle Time per Crossing (min) — flag > 30min",
    f"""SELECT date_time2 AS time, site AS metric, cycle_seconds/60.0 AS value
        FROM tickets
        WHERE {FILTER} AND $__timeFilter(date_time2)
        ORDER BY date_time2""",
    x=0, y=28, w=24, h=8, unit="short",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None},
        {"color": "red", "value": 30},
    ]})); pid += 1

# --- Row: Weighbridge activity timeline (Top N trucks only) ---
# Scoped to the same Top-N busiest trucks as the per-truck bar charts above
# — at full cardinality (up to 113 trucks at Dublin) a per-truck timeline
# is unreadable and this panel would be useless.
panels.append(state_timeline_panel(pid, f"Weighbridge Activity by Truck — Top {TOP_N}",
    f"""SELECT date_time AS time, vehicle_num AS metric, 'At weighbridge' AS value
        FROM tickets WHERE {FILTER} AND vehicle_num IN {TOP_N_TRUCKS_SUBQUERY}
        UNION ALL
        SELECT date_time2 AS time, vehicle_num AS metric, 'Idle' AS value
        FROM tickets WHERE {FILTER} AND vehicle_num IN {TOP_N_TRUCKS_SUBQUERY}
        ORDER BY time""",
    x=0, y=36)); pid += 1

# --- Row: Net weight & cycle time per tip, by truck (Top N) ---
panels.append(dual_axis_timeseries_panel(
    pid, f"Net Weight & Cycle Time per Tip, by Truck — Top {TOP_N}",
    sql_left=f"""SELECT date_time2 AS time,
                        vehicle_num || ' - weight (t)' AS metric,
                        net_weight AS value
                 FROM tickets
                 WHERE {FILTER} AND vehicle_num IN {TOP_N_TRUCKS_SUBQUERY}
                       AND $__timeFilter(date_time2)
                 ORDER BY date_time2""",
    sql_right=f"""SELECT date_time2 AS time,
                         vehicle_num || ' - cycle (min)' AS metric,
                         cycle_seconds/60.0 AS value
                  FROM tickets
                  WHERE {FILTER} AND vehicle_num IN {TOP_N_TRUCKS_SUBQUERY}
                        AND $__timeFilter(date_time2)
                  ORDER BY date_time2""",
    right_match_regex=".*cycle.*",
    x=0, y=44, w=24, h=9,
    unit_left="short", unit_right="m",
)); pid += 1

# --- Row: Data quality flags ---
# Four checks, each independently justified against the combined dataset:
#   1. weight_mismatch > 0.01  -> net_weight_reported doesn't reconcile
#      with |weight1 - weight2|. 9 rows, all at Foynes.
#   2. cycle_minutes > 30      -> same outlier threshold as above,
#      surfaced per-ticket (179/7056 rows, ~2.5%).
#   3. net_weight < 10         -> partial/aborted load; may be genuine
#      (e.g. a final top-up load) rather than an error — flagged for
#      review, not auto-corrected.
#   4. cycle_time_reversed     -> date_time2 was before date_time in the
#      source data; cycle_seconds already stores abs(), so this flag is
#      the only remaining trace that the correction was applied. Mostly
#      isolated rows per site, EXCEPT Lisahally, where ~32% of tickets
#      have date_time2 == date_time (zero-duration, not reversed) — a
#      likely logging limitation of that site's system rather than
#      scattered bad data. This flag only catches true reversals
#      (date_time2 < date_time), not the zero-duration case, so it will
#      NOT surface all of Lisahally's rows here — that pattern is a
#      site-level note, not a per-ticket anomaly.
panels.append(table_panel(pid, "Data Quality Flags",
    f"""SELECT site, ticket, vehicle_num, voyage, ticket_type, date_time, date_time2,
               weight1, weight2, net_weight,
               ROUND((cycle_seconds/60.0)::numeric, 1) AS cycle_minutes,
               ROUND(weight_mismatch::numeric, 2) AS weight_mismatch,
               cycle_time_reversed
        FROM tickets
        WHERE {FILTER}
          AND (
                weight_mismatch > 0.01
             OR cycle_seconds > 1800
             OR net_weight < 10
             OR cycle_time_reversed
          )
        ORDER BY date_time""",
    x=0, y=53)); pid += 1

dashboard = {
    "title": "Despatch and Demurrage",
    "uid": "despatch-and-demurrage",
    "schemaVersion": 39,
    "version": 1,
    "editable": True,
    "time": {"from": "now-90d", "to": "now"},
    "timezone": "browser",
    "panels": panels,
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
                          "ORDER BY 1"),
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
        ]
    },
}

# The voyage list follows the time picker, so it can come back empty;
# see with_placeholder. sort: 1 = alphabetical asc.
for v in dashboard["templating"]["list"]:
    if v["name"] == "voyage":
        v["query"] = with_placeholder(v["query"], "(no voyage in window)")
        v["sort"] = 1

with open("despatch-and-demurrage.json", "w") as f:
    json.dump(dashboard, f, indent=2)

print("Wrote despatch-and-demurrage.json")
print(f"Total panels: {len(panels)}")

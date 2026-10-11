"""
Load one or more weighbridge ticket exports (one file per site) into
Postgres. Replaces the earlier VesselSample-based schema entirely — that
was a placeholder built before real data existed; this matches the real
multi-site export format (Dublin, Foynes, Lisahally, Ringaskiddy, ...).

Usage:
    python load_data.py /path/to/dublin_august_2026.xlsx /path/to/foynes_august_2026.xlsx ...

Safe to re-run: rows are upserted on (site, ticket), so re-loading the
same or overlapping exports never creates duplicates. `site` comes from
the data itself (the "Site" column), not the filename.

Known schema quirks handled here (see README for the full data-quality
notes these were derived from):
  - Most sites report net weight in a column called "Weight3"; Foynes
    calls the same thing "Weight4". Both are read into net_weight_reported.
  - net_weight is independently computed as |Weight - Weight2| and compared
    against net_weight_reported (see weight_mismatch) rather than trusted
    blindly — Foynes has 9 rows where they disagree.
  - Date+Time2 is occasionally before Date+Time (rare, a handful of rows
    per site) or equal to it (common at Lisahally specifically — ~32% of
    that site's rows have zero-duration tickets, most likely a logging
    limitation of that site's weighbridge system rather than corrupt data
    scattered everywhere). cycle_seconds is stored as an absolute value so
    downstream duration math doesn't break, but cycle_time_reversed flags
    which rows that correction was applied to, so the underlying issue
    stays visible rather than silently disappearing.
"""
import sys
import pandas as pd
from sqlalchemy import create_engine, text

DB_URL = "postgresql+psycopg2://vessel:vessel@localhost:5432/vessel"

# The very first version of this loader created a `tickets` table shaped
# around the single-site VesselSample.xlsx placeholder (customer, commodity,
# hold, veh_reg, ...). That schema is superseded by the one below, so drop
# it unconditionally rather than silently failing on mismatched columns.
# This deletes only that early sample-derived table — none of your real
# site exports are lost, since they haven't been loaded under the old
# schema.
DROP_OLD_TABLE_SQL = "DROP TABLE IF EXISTS tickets;"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS tickets (
    site                TEXT NOT NULL,
    ticket              TEXT NOT NULL,
    voyage              TEXT,
    ticket_type         TEXT,
    trailer_num         TEXT,
    vehicle_num         TEXT,
    equipment           TEXT,
    item_number         TEXT,
    location            TEXT,
    date_time           TIMESTAMP,
    date_time2          TIMESTAMP,
    weight1             DOUBLE PRECISION,
    weight2             DOUBLE PRECISION,
    net_weight_reported DOUBLE PRECISION,
    net_weight          DOUBLE PRECISION,
    weight_mismatch     DOUBLE PRECISION,
    cycle_seconds       DOUBLE PRECISION,
    cycle_time_reversed BOOLEAN,
    PRIMARY KEY (site, ticket)
);
"""

# Each bay's max capacity is a fixed physical constant that never appears
# in a weighbridge ticket, so it lives in its own reference table. Every
# VESSEL-IN bay seen in the data gets a placeholder row (5000t) the first
# time it appears; ON CONFLICT DO NOTHING means re-running the loader never
# overwrites a real figure you've since entered by hand. store_name is the
# same story: the exports only carry a bay code, so it's left NULL for you
# to fill in, and the dashboard shows the bay code until you do.
CREATE_BAY_CAPACITY_SQL = """
CREATE TABLE IF NOT EXISTS bay_capacity (
    site         TEXT NOT NULL,
    bay          TEXT NOT NULL,
    max_capacity DOUBLE PRECISION NOT NULL DEFAULT 5000,
    store_name   TEXT,
    PRIMARY KEY (site, bay)
);
ALTER TABLE bay_capacity ADD COLUMN IF NOT EXISTS store_name TEXT;
"""

SEED_BAY_CAPACITY_SQL = """
INSERT INTO bay_capacity (site, bay)
SELECT DISTINCT site, location FROM tickets
WHERE ticket_type = 'VESSEL-IN' AND location IS NOT NULL
ON CONFLICT (site, bay) DO NOTHING;
"""

UPSERT_SQL = """
INSERT INTO tickets (
    site, ticket, voyage, ticket_type, trailer_num, vehicle_num, equipment,
    item_number, location, date_time, date_time2, weight1, weight2,
    net_weight_reported, net_weight, weight_mismatch, cycle_seconds,
    cycle_time_reversed
) VALUES (
    :site, :ticket, :voyage, :ticket_type, :trailer_num, :vehicle_num, :equipment,
    :item_number, :location, :date_time, :date_time2, :weight1, :weight2,
    :net_weight_reported, :net_weight, :weight_mismatch, :cycle_seconds,
    :cycle_time_reversed
)
ON CONFLICT (site, ticket) DO UPDATE SET
    voyage = EXCLUDED.voyage,
    ticket_type = EXCLUDED.ticket_type,
    trailer_num = EXCLUDED.trailer_num,
    vehicle_num = EXCLUDED.vehicle_num,
    equipment = EXCLUDED.equipment,
    item_number = EXCLUDED.item_number,
    location = EXCLUDED.location,
    date_time = EXCLUDED.date_time,
    date_time2 = EXCLUDED.date_time2,
    weight1 = EXCLUDED.weight1,
    weight2 = EXCLUDED.weight2,
    net_weight_reported = EXCLUDED.net_weight_reported,
    net_weight = EXCLUDED.net_weight,
    weight_mismatch = EXCLUDED.weight_mismatch,
    cycle_seconds = EXCLUDED.cycle_seconds,
    cycle_time_reversed = EXCLUDED.cycle_time_reversed;
"""


def parse_file(path: str) -> pd.DataFrame:
    df = pd.read_excel(path)

    # Net-weight column is named Weight3 at most sites, Weight4 at Foynes.
    net_col = "Weight4" if "Weight4" in df.columns else "Weight3"

    df = df.rename(columns={
        "Site": "site",
        "Weighticket": "ticket",
        "Voyage": "voyage",
        "Weighticket type": "ticket_type",
        "Trailer Num": "trailer_num",
        "Vehicle Num": "vehicle_num",
        "Equipment": "equipment",
        "Item number": "item_number",
        "Loading/Tipping Location": "location",
        "Date+Time": "date_time",
        "Date+Time2": "date_time2",
        "Weight": "weight1",
        "Weight2": "weight2",
        net_col: "net_weight_reported",
    })

    # Coerce ID-like columns to plain strings. Vehicle/trailer regs mix
    # letters and digits so this is mostly a no-op, but pandas will infer
    # a numeric dtype for a column if every value in one file happens to
    # look numeric (e.g. an all-digit trailer number), which then either
    # mangles the value (leading zeros dropped, ".0" appended) or breaks
    # type consistency once concatenated with a file where the same
    # column has letters.
    #
    # Deliberately NOT using pandas' nullable "string" dtype here
    # (df[c].astype("string")) — that extension dtype needs pandas >= 1.0
    # and raises `TypeError: data type 'string' not understood` on older
    # installs (numpy's dtype resolver gets asked to make sense of it and
    # fails). object dtype + an explicit per-value str() cast works
    # identically for our purposes on any pandas version.
    for c in ("site", "ticket", "voyage", "ticket_type", "trailer_num",
              "vehicle_num", "equipment", "item_number", "location"):
        df[c] = df[c].apply(lambda v: None if pd.isna(v) else str(v))

    df["net_weight"] = (df["weight1"] - df["weight2"]).abs()
    df["weight_mismatch"] = (df["net_weight"] - df["net_weight_reported"]).abs()

    raw_diff = (df["date_time2"] - df["date_time"]).dt.total_seconds()
    df["cycle_time_reversed"] = raw_diff < 0
    df["cycle_seconds"] = raw_diff.abs()

    cols = ["site", "ticket", "voyage", "ticket_type", "trailer_num", "vehicle_num",
            "equipment", "item_number", "location", "date_time", "date_time2",
            "weight1", "weight2", "net_weight_reported", "net_weight",
            "weight_mismatch", "cycle_seconds", "cycle_time_reversed"]
    return df[cols]


def seed_bay_capacity(engine) -> int:
    """Create bay_capacity if missing and add placeholder rows for new bays."""
    with engine.begin() as conn:
        conn.execute(text(CREATE_BAY_CAPACITY_SQL))
        return conn.execute(text(SEED_BAY_CAPACITY_SQL)).rowcount


def main():
    if len(sys.argv) < 2:
        print("Usage: python load_data.py file1.xlsx [file2.xlsx ...]")
        sys.exit(1)

    engine = create_engine(DB_URL)

    with engine.begin() as conn:
        # One-time migration: if a `tickets` table already exists from the
        # old single-site sample schema (no `site` column), drop it before
        # recreating. This only ever fires once — after the first run under
        # this schema, the `site` column exists and this is a no-op, so
        # loading additional site files afterward never wipes prior data.
        exists = conn.execute(text(
            "SELECT to_regclass('public.tickets') IS NOT NULL"
        )).scalar()
        if exists:
            has_site_col = conn.execute(text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name='tickets' AND column_name='site')"
            )).scalar()
            if not has_site_col:
                print("Migrating: dropping old single-site 'tickets' table "
                      "(sample schema) before creating the new one.")
                conn.execute(text(DROP_OLD_TABLE_SQL))
        conn.execute(text(CREATE_TABLE_SQL))

    total = 0
    for path in sys.argv[1:]:
        df = parse_file(path)
        with engine.begin() as conn:
            for row in df.to_dict(orient="records"):
                conn.execute(text(UPSERT_SQL), row)
        print(f"Loaded {len(df)} rows from {path}")
        total += len(df)

    print(f"Total: {total} rows across {len(sys.argv) - 1} file(s).")
    print(f"bay_capacity: {seed_bay_capacity(engine)} new bay(s) seeded at 5000t placeholder.")


if __name__ == "__main__":
    main()

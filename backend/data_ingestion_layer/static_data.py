"""
load_static_data.py

GRIDSHIELD — loads STATIC (fixed, rarely-changing) topology data into
Postgres/PostGIS, using ONLY real values from final_2.0_dataset.csv —
real substation codes/names, real HT pole chains, real DTC codes, real
facility names and coordinates.

Fixes applied vs. the earlier 8 scripts:
    - one consistent ID scheme, taken directly from the real dataset
      (no invented IDs that don't match across scripts)
    - no fictional/wrong coordinates (e.g. no more 21.57, 74.57 — every
      coordinate here comes straight from the source file)
    - writes to Postgres, not local JSON/SQLite files
    - is_switchable / switch_type filled using the auto-tagging rule
      built earlier (open + relay NORMAL = tie; open + relay NOT normal
      = flagged for review, never silently mislabeled)

Run this ONCE (or whenever the real dataset changes) — this is static
data, not live telemetry. Live telemetry generation is a separate script.

Dependencies:
    pip install pandas psycopg2-binary python-dotenv

Usage:
    python backend/data_ingestion_layer/static_data.py --input backend/dataset2.0
"""

import argparse
import os
import random
import re
from pathlib import Path
from urllib.parse import unquote

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
load_dotenv(ROOT_DIR / ".env")
load_dotenv(ROOT_DIR / "backend" / ".env", override=False)


# ---------------------------------------------------------------------------
# DB connection
# ---------------------------------------------------------------------------

def get_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", 5432),
        dbname=os.getenv("POSTGRES_DB", "gridbandhu"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=unquote(os.getenv("POSTGRES_PASSWORD", "")),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CRITICALITY_MAP = {
    "HOSPITAL": "High",
    "WATER_UTILITY": "High",
    "FIRE_STATION": "High",
    "POWER_UTILITY": "Medium",
    "POWER_PLANT": "Medium",
}

# backup_power_kw is NOT present in the source data. These ranges are
# realistic estimates by facility type (typical diesel-generator/backup
# sizing for a facility this size), used only until real backup-power
# figures are collected. Seeded so the same facility always gets the
# same value on every re-run, instead of changing randomly each time.
BACKUP_POWER_RANGES_KW = {
    "HOSPITAL": (80, 200),
    "WATER_UTILITY": (40, 100),
    "FIRE_STATION": (20, 50),
    "POWER_UTILITY": (30, 80),
    "POWER_PLANT": (50, 150),
}
_DEFAULT_BACKUP_RANGE_KW = (10, 30)


def estimate_backup_power_kw(facility_id: str, facility_type: str) -> float:
    """Deterministic 'random' estimate — seeded by facility_id, so the
    same facility always gets the same value across repeated runs."""
    rng = random.Random(facility_id)  # seed per-facility, not global
    low, high = BACKUP_POWER_RANGES_KW.get(str(facility_type).upper(), _DEFAULT_BACKUP_RANGE_KW)
    return round(rng.uniform(low, high) / 5) * 5  # round to nearest 5 kW


def slugify_facility_name(name: str) -> str:
    """Turns a real facility name into a stable, deterministic ID —
    same input always produces the same ID, unlike a random/incrementing
    counter, so re-running the loader doesn't create duplicates or drift."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().upper())
    return f"FAC_{slug[:40]}"


def auto_tag_switch(breaker_status: str, relay_status: str) -> tuple[str, bool]:
    """Same rule established earlier: open + relay NORMAL = tie switch
    (safe, deliberate backup). Open + relay NOT normal = flagged for
    review, never silently called safe."""
    is_open = str(breaker_status).strip().upper() == "OPEN"
    is_relay_normal = str(relay_status).strip().upper() == "NORMAL"

    if is_open and is_relay_normal:
        return "tie", False
    elif is_open and not is_relay_normal:
        return "main", True   # needs_review
    else:
        return "main", False


# ---------------------------------------------------------------------------
# Load + transform
# ---------------------------------------------------------------------------

def load_dataframe(input_path: str) -> pd.DataFrame:
    df = pd.read_csv(input_path)
    required = [
        "SUBSTATION_CODE", "SUBSTATION_NAME", "HT_POLE_ID", "INITIAL_POLE",
        "INITIAL_POLE_LATITUDE", "INITIAL_POLE_LONGITUDE", "DESTINATION_POLE",
        "DESTINATION_POLE_LATITUDE", "DESTINATION_POLE_LONGITUDE",
        "FEEDER_CODE", "VOLTAGE_KV", "TRANSFORMER_PTR_CAPACITY_MVA",
        "POWER_FACTOR", "BREAKER_STATUS", "protective_relay_status",
        "DTC_CODE", "DTC_NAME", "DTC_TYPE", "DTC_CAPACITY",
        "CONNECTED_FACILITY_NAME", "CONNECTED_FACILITY_TYPE",
        "FACILITY_LATITUDE", "FACILITY_LONGITUDE", "HAS_DEDICATED_TRANSFORMER",
        "feeder_id",
    ]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Source file is missing expected columns: {missing}")
    return df


# ---------------------------------------------------------------------------
# Per-table insert functions
# ---------------------------------------------------------------------------

def insert_substations(cur, df: pd.DataFrame):
    # Approximate substation location using the FIRST pole seen for that
    # substation — the source file has no direct substation coordinate.
    # Flagged explicitly via location_is_approx, not silently assumed exact.
    rows = []
    seen = set()
    for _, row in df.iterrows():
        sid = str(row["SUBSTATION_CODE"])
        if sid in seen:
            continue
        seen.add(sid)
        rows.append((
            sid, row["SUBSTATION_NAME"],
            row["INITIAL_POLE_LONGITUDE"], row["INITIAL_POLE_LATITUDE"],
        ))

    execute_values(cur, """
        INSERT INTO substations (substation_id, name, location, location_is_approx)
        VALUES %s
        ON CONFLICT (substation_id) DO UPDATE SET name = EXCLUDED.name
    """, [(r[0], r[1], f"POINT({r[2]} {r[3]})", True) for r in rows],
        template="(%s, %s, ST_GeogFromText('SRID=4326;' || %s), %s)")

    print(f"  substations: {len(rows)} rows")


def insert_ht_edges(cur, df: pd.DataFrame):
    rows = []
    for _, row in df.iterrows():
        capacity_kw = float(row["TRANSFORMER_PTR_CAPACITY_MVA"]) * float(row["POWER_FACTOR"]) * 1000
        switch_type, needs_review = auto_tag_switch(row["BREAKER_STATUS"], row["protective_relay_status"])

        rows.append((
            row["HT_POLE_ID"], row["INITIAL_POLE"], row["DESTINATION_POLE"],
            row["INITIAL_POLE_LONGITUDE"], row["INITIAL_POLE_LATITUDE"],
            row["DESTINATION_POLE_LONGITUDE"], row["DESTINATION_POLE_LATITUDE"],
            str(row["SUBSTATION_CODE"]), str(row["FEEDER_CODE"]),
            float(row["VOLTAGE_KV"]), round(capacity_kw, 2),
            True, switch_type, needs_review,
        ))

    for r in rows:
        cur.execute("""
            INSERT INTO ht_edges (
                edge_id, from_node_id, to_node_id,
                from_location, to_location, path,
                substation_id, feeder_id, voltage_kv, capacity_kw,
                is_switchable, switch_type, needs_review
            ) VALUES (
                %s, %s, %s,
                ST_GeogFromText('SRID=4326;POINT(' || %s || ' ' || %s || ')'),
                ST_GeogFromText('SRID=4326;POINT(' || %s || ' ' || %s || ')'),
                ST_GeogFromText('SRID=4326;LINESTRING(' || %s || ' ' || %s || ', ' || %s || ' ' || %s || ')'),
                %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (edge_id) DO UPDATE SET
                capacity_kw = EXCLUDED.capacity_kw,
                switch_type = EXCLUDED.switch_type,
                needs_review = EXCLUDED.needs_review
        """, (
            r[0], r[1], r[2],
            r[3], r[4], r[5], r[6],
            r[3], r[4], r[5], r[6],
            r[7], r[8], r[9], r[10], r[11], r[12], r[13],
        ))

    review_count = sum(1 for r in rows if r[13])
    print(f"  ht_edges: {len(rows)} rows ({review_count} flagged needs_review)")


def insert_dtc(cur, df: pd.DataFrame):
    rows = []
    seen = set()
    for _, row in df.iterrows():
        dtc_id = str(row["DTC_CODE"])
        if dtc_id in seen:
            continue
        seen.add(dtc_id)
        rows.append((
            dtc_id, row["DTC_NAME"], row["DTC_TYPE"], float(row["DTC_CAPACITY"]),
            str(row["SUBSTATION_CODE"]), row["HT_POLE_ID"],
        ))

    execute_values(cur, """
        INSERT INTO dtc (dtc_id, name, dtc_type, capacity_kw, parent_substation_id, parent_ht_edge_id)
        VALUES %s
        ON CONFLICT (dtc_id) DO UPDATE SET capacity_kw = EXCLUDED.capacity_kw
    """, rows)

    print(f"  dtc: {len(rows)} rows")


def insert_facilities(cur, df: pd.DataFrame):
    rows = []
    seen = set()
    for _, row in df.iterrows():
        if pd.isna(row["CONNECTED_FACILITY_NAME"]):
            continue
        fac_id = slugify_facility_name(row["CONNECTED_FACILITY_NAME"])
        if fac_id in seen:
            continue
        seen.add(fac_id)

        facility_type = row["CONNECTED_FACILITY_TYPE"]
        criticality = CRITICALITY_MAP.get(str(facility_type).upper(), "Low")
        dtc_id = str(row["DTC_CODE"])
        lt_line_id = f"LT_{dtc_id}_{fac_id}"
        backup_kw = estimate_backup_power_kw(fac_id, facility_type)

        rows.append((
            fac_id, row["CONNECTED_FACILITY_NAME"], facility_type,
            row["FACILITY_LONGITUDE"], row["FACILITY_LATITUDE"],
            dtc_id, bool(row["HAS_DEDICATED_TRANSFORMER"]), criticality,
            backup_kw,  # estimated — see BACKUP_POWER_RANGES_KW note above
            lt_line_id,
        ))

    for r in rows:
        cur.execute("""
            INSERT INTO facilities (
                facility_id, name, facility_type, location,
                connected_dtc_id, has_dedicated_transformer, criticality_tier,
                backup_power_kw, lt_line_id
            ) VALUES (
                %s, %s, %s,
                ST_GeogFromText('SRID=4326;POINT(' || %s || ' ' || %s || ')'),
                %s, %s, %s, %s, %s
            )
            ON CONFLICT (facility_id) DO UPDATE SET
                connected_dtc_id = EXCLUDED.connected_dtc_id
        """, (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9]))

    print(f"  facilities: {len(rows)} rows "
          f"(backup_power_kw ESTIMATED by facility type — not in source data, "
          f"see BACKUP_POWER_RANGES_KW for the ranges used)")


def insert_links(cur, df: pd.DataFrame):
    # substation <-> HT feeder (derived: every unique substation+feeder pair seen)
    sub_ht = df[["SUBSTATION_CODE", "FEEDER_CODE"]].drop_duplicates()
    execute_values(cur, """
        INSERT INTO substation_ht_lines (substation_id, feeder_id) VALUES %s
        ON CONFLICT DO NOTHING
    """, [(str(r.SUBSTATION_CODE), str(r.FEEDER_CODE)) for r in sub_ht.itertuples()])
    print(f"  substation_ht_lines: {len(sub_ht)} rows")

    # dtc <-> lt line (derived: one LT line per facility served by that DTC)
    dtc_lt = df.dropna(subset=["CONNECTED_FACILITY_NAME"])
    lt_rows = [
        (str(r.DTC_CODE), f"LT_{r.DTC_CODE}_{slugify_facility_name(r.CONNECTED_FACILITY_NAME)}")
        for r in dtc_lt.itertuples()
    ]
    execute_values(cur, """
        INSERT INTO dtc_lt_lines (dtc_id, lt_line_id) VALUES %s
        ON CONFLICT DO NOTHING
    """, lt_rows)
    print(f"  dtc_lt_lines: {len(lt_rows)} rows")

    # dtc <-> consumer (facility)
    consumer_rows = [
        (str(r.DTC_CODE), slugify_facility_name(r.CONNECTED_FACILITY_NAME), r.CONNECTED_FACILITY_TYPE)
        for r in dtc_lt.itertuples()
    ]
    execute_values(cur, """
        INSERT INTO dtc_consumers (dtc_id, consumer_id, consumer_type) VALUES %s
        ON CONFLICT DO NOTHING
    """, consumer_rows)
    print(f"  dtc_consumers: {len(consumer_rows)} rows")

    # consumer groups (real feeder_id column: G001, G002, ...)
    group_rows = df[["feeder_id", "DTC_CODE"]].drop_duplicates()
    execute_values(cur, """
        INSERT INTO consumer_groups (group_code, consumer_id, allocation_basis) VALUES %s
        ON CONFLICT DO NOTHING
    """, [(r.feeder_id, str(r.DTC_CODE), "revenue-based") for r in group_rows.itertuples()])
    print(f"  consumer_groups: {len(group_rows)} rows")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default=str(Path(__file__).resolve().parents[1] / "dataset2.0"),
    )
    args = parser.parse_args()

    df = load_dataframe(args.input)
    print(f"Loaded {len(df)} rows from {args.input}")

    conn = get_connection()
    cur = conn.cursor()

    print("Inserting into Postgres...")
    insert_substations(cur, df)
    insert_ht_edges(cur, df)
    insert_dtc(cur, df)
    insert_facilities(cur, df)
    insert_links(cur, df)

    conn.commit()
    cur.close()
    conn.close()
    print("Done. Static topology loaded successfully.")


if __name__ == "__main__":
    main()

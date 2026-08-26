"""
generate_live_telemetry.py

GRIDSHIELD — live telemetry generator (Redis).

Reads REAL structural facts from Postgres (capacity, switch type, which
DTC belongs to which substation, etc.) and uses them as the baseline for
continuously-updating live readings, pushed into Redis.

This replaces reading final_2.0_dataset.csv / dataset2.0 directly for
live data — Postgres is now the source of truth for structure, this
script only generates the CHANGING numbers on top of it.

Covers 4 entity types, each with its own live fields:
    - ht_edges     (feeders/poles)  -> current_flow_kw, switch_state,
                                        protective_relay_status
                                        (this is what trigger_detection.py
                                        already reads and understands)
    - dtc          (transformers)   -> current_load_kw, loading_percentage
    - facilities   (hospitals etc.) -> current_load_kw, avg_utilised_power_1min
    - substations                   -> current_load_kw

Dependencies:
    pip install psycopg2-binary redis apscheduler python-dotenv

Usage:
    python generate_live_telemetry.py --interval-seconds 30
"""

import argparse
import os
import random

import psycopg2
import redis
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv

load_dotenv()


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def get_pg_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", 5432),
        dbname=os.getenv("POSTGRES_DB", "gridbandhu"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
    )


def get_redis_client():
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        db=int(os.getenv("REDIS_DB", 0)),
        decode_responses=True,
    )


r = get_redis_client()

# NOTE: this generator does NOT decide faults. It only produces realistic,
# continuously-changing numbers. Deciding what counts as a fault is the
# Detection layer's job (trigger_detection.py) — it compares these live
# numbers against the real structural rules (capacity, expected switch
# state, relay status) and makes that call independently. Keeping that
# decision out of this script is deliberate, not an oversight.


# ---------------------------------------------------------------------------
# Step 1 — Load REAL structural baselines from Postgres (once, at startup)
# ---------------------------------------------------------------------------

def load_baselines():
    conn = get_pg_connection()
    cur = conn.cursor()

    baselines = {"edges": {}, "dtc": {}, "facilities": {}, "substations": {}}

    # HT edges (feeders/poles) — real capacity + switch_type from Postgres.
    # needs_review is pulled in too, so a genuinely flagged real anomaly
    # (from the original dataset) is reflected honestly, instead of being
    # silently overwritten by "normal" fluctuating numbers.
    cur.execute("SELECT edge_id, capacity_kw, switch_type, needs_review FROM ht_edges")
    for edge_id, capacity_kw, switch_type, needs_review in cur.fetchall():
        baselines["edges"][edge_id] = {
            "capacity_kw": float(capacity_kw),
            "expected_state": "closed" if switch_type == "main" else "open",
            "needs_review": bool(needs_review),
        }

    # DTCs — real capacity
    cur.execute("SELECT dtc_id, capacity_kw FROM dtc")
    for dtc_id, capacity_kw in cur.fetchall():
        baselines["dtc"][dtc_id] = {"capacity_kw": float(capacity_kw)}

    # Facilities — real backup power (used as a proxy for typical load size)
    cur.execute("SELECT facility_id, facility_type, backup_power_kw FROM facilities")
    for facility_id, facility_type, backup_power_kw in cur.fetchall():
        baselines["facilities"][facility_id] = {
            "facility_type": facility_type,
            "typical_load_kw": float(backup_power_kw) * 1.4,  # normal load usually exceeds backup sizing
        }

    # Substations — capacity derived as the sum of their DTCs' capacities
    # (the source dataset never gave a direct substation capacity figure)
    cur.execute("""
        SELECT s.substation_id, COALESCE(SUM(d.capacity_kw), 0)
        FROM substations s
        LEFT JOIN dtc d ON d.parent_substation_id = s.substation_id
        GROUP BY s.substation_id
    """)
    for substation_id, capacity_kw in cur.fetchall():
        baselines["substations"][substation_id] = {"capacity_kw": float(capacity_kw)}

    cur.close()
    conn.close()
    return baselines


# ---------------------------------------------------------------------------
# Step 2 — Small helper for realistic variation (no fault decisions here)
# ---------------------------------------------------------------------------

def nudge(value: float, pct: float = 0.03) -> float:
    return round(value * (1 + random.uniform(-pct, pct)), 2)


# ---------------------------------------------------------------------------
# Step 3 — Generate + push one cycle for each entity type
# ---------------------------------------------------------------------------

def update_edges(edges: dict):
    for edge_id, base in edges.items():
        key = f"edge:{edge_id}"

        if base["needs_review"]:
            # This edge genuinely showed an anomaly in the real source
            # data (open switch, relay not normal). Keep reflecting that
            # honestly — this is a REAL condition, not a simulated one.
            # It's still Detection's job to decide this counts as a fault.
            reading = {
                "current_flow_kw": 0,
                "switch_state": "open",
                "protective_relay_status": "PICKUP",
            }
        else:
            baseline_flow = base["capacity_kw"] * 0.6 if base["expected_state"] == "closed" else 0
            reading = {
                "current_flow_kw": nudge(baseline_flow) if baseline_flow else 0,
                "switch_state": base["expected_state"],
                "protective_relay_status": "NORMAL",
            }

        r.hset(key, mapping=reading)


def update_dtc(dtc: dict):
    for dtc_id, base in dtc.items():
        load = nudge(base["capacity_kw"] * random.uniform(0.5, 0.8))
        r.hset(f"node:{dtc_id}", mapping={
            "current_load_kw": load,
            "loading_percentage": round((load / base["capacity_kw"]) * 100, 1) if base["capacity_kw"] else 0,
            "status": "good",
        })


def update_facilities(facilities: dict):
    for facility_id, base in facilities.items():
        load = nudge(base["typical_load_kw"] * random.uniform(0.6, 0.9))
        r.hset(f"node:{facility_id}", mapping={
            "current_load_kw": load,
            "avg_utilised_power_1min": nudge(load, pct=0.01),
            "status": "good",
        })


def update_substations(substations: dict):
    for sub_id, base in substations.items():
        load = nudge(base["capacity_kw"] * random.uniform(0.45, 0.75)) if base["capacity_kw"] else 0
        r.hset(f"node:{sub_id}", mapping={"current_load_kw": load, "status": "good"})


def run_cycle(baselines: dict):
    print("Running live telemetry cycle...")
    update_edges(baselines["edges"])
    update_dtc(baselines["dtc"])
    update_facilities(baselines["facilities"])
    update_substations(baselines["substations"])
    print("Cycle done.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval-seconds", type=int, default=30)
    args = parser.parse_args()

    baselines = load_baselines()
    print(f"Loaded real baselines from Postgres: "
          f"{len(baselines['edges'])} edges, {len(baselines['dtc'])} DTCs, "
          f"{len(baselines['facilities'])} facilities, {len(baselines['substations'])} substations.")

    scheduler = BlockingScheduler()
    scheduler.add_job(run_cycle, "interval", seconds=args.interval_seconds, args=[baselines])
    run_cycle(baselines)
    scheduler.start()


if __name__ == "__main__":
    main()
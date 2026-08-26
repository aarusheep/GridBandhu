"""
live_data.py

GRIDBANDHU / GRIDSHIELD — Live Telemetry Generator

PostgreSQL:
    Source of truth for GRID STRUCTURE.

Redis:
    Source of truth for LIVE TELEMETRY.

The generator reads structural information from PostgreSQL and
continuously generates changing live readings in Redis.

Entity types:

    HT edges
        edge:<edge_id>
        - current_flow_kw
        - switch_state
        - protective_relay_status

    DTCs
        node:<dtc_id>
        - current_load_kw
        - loading_percentage
        - status

    Facilities
        node:<facility_id>
        - current_load_kw
        - avg_utilised_power_1min
        - status

    Substations
        node:<substation_id>
        - current_load_kw
        - status

IMPORTANT:

    PostgreSQL is the source of truth for valid IDs.

    Redis stale telemetry is automatically removed.

    This script does NOT decide whether something is a fault.
    Detection is handled by backend/Detection/detection.py.
"""

import argparse
import os
import random
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

import psycopg2
import redis

from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv


# ============================================================================
# ENVIRONMENT
# ============================================================================

# Project structure:
#
# GRIDBANDHU/
# └── backend/
#     ├── .env
#     └── data_ingestion_layer/
#         └── live_data.py
#
# parents[1] points to backend/, where the shared .env file lives.

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
if not ENV_FILE.is_file():
    raise RuntimeError(f"Required environment file not found: {ENV_FILE}")
load_dotenv(ENV_FILE, override=False)


# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================================
# CONFIGURATION
# ============================================================================

POSTGRES_HOST = os.getenv(
    "POSTGRES_HOST",
    "localhost",
)

POSTGRES_PORT = int(
    os.getenv(
        "POSTGRES_PORT",
        "5432",
    )
)

POSTGRES_DB = os.getenv(
    "POSTGRES_DB",
    "gridbandhu",
)

POSTGRES_USER = os.getenv(
    "POSTGRES_USER",
    "postgres",
)

POSTGRES_PASSWORD = os.getenv(
    "POSTGRES_PASSWORD",
    "",
)

REDIS_HOST = os.getenv(
    "REDIS_HOST",
    "localhost",
)

REDIS_PORT = int(
    os.getenv(
        "REDIS_PORT",
        "6379",
    )
)

REDIS_DB = int(
    os.getenv(
        "REDIS_DB",
        "0",
    )
)


# ============================================================================
# REDIS CONNECTION
# ============================================================================

def get_redis_client():
    """
    Create Redis client using values from .env.
    """

    client = redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        decode_responses=True,
        protocol=2,
    )

    # Test connection immediately.
    client.ping()

    return client


# Create Redis client once.
r = get_redis_client()


# ============================================================================
# POSTGRES CONNECTION
# ============================================================================

def get_pg_connection():
    """
    Create PostgreSQL connection using values from .env.
    """

    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=unquote(POSTGRES_PASSWORD),
    )


# ============================================================================
# DATABASE CONNECTION TEST
# ============================================================================

def test_connections():
    """
    Verify PostgreSQL and Redis before starting telemetry generation.
    """

    logger.info("Testing PostgreSQL connection...")

    conn = get_pg_connection()

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database();")
            database_name = cur.fetchone()[0]

        logger.info(
            "PostgreSQL connected successfully: %s",
            database_name,
        )

    finally:
        conn.close()

    logger.info("Testing Redis connection...")

    r.ping()

    logger.info(
        "Redis connected successfully: %s:%s / DB %s",
        REDIS_HOST,
        REDIS_PORT,
        REDIS_DB,
    )


# ============================================================================
# LOAD STRUCTURAL BASELINES
# ============================================================================

def load_baselines():
    """
    Load real structural information from PostgreSQL.

    PostgreSQL is the source of truth.

    Returns:

        {
            "edges": {},
            "dtc": {},
            "facilities": {},
            "substations": {}
        }
    """

    conn = get_pg_connection()

    baselines = {
        "edges": {},
        "dtc": {},
        "facilities": {},
        "substations": {},
    }

    try:

        with conn.cursor() as cur:

            # ----------------------------------------------------------------
            # HT EDGES
            # ----------------------------------------------------------------

            cur.execute(
                """
                SELECT
                    edge_id,
                    capacity_kw,
                    switch_type,
                    needs_review
                FROM ht_edges
                """
            )

            for (
                edge_id,
                capacity_kw,
                switch_type,
                needs_review,
            ) in cur.fetchall():

                # Skip malformed records.
                if edge_id is None:
                    continue

                edge_id = str(edge_id)

                capacity = (
                    float(capacity_kw)
                    if capacity_kw is not None
                    else 0.0
                )

                switch_type = (
                    str(switch_type).lower()
                    if switch_type is not None
                    else "main"
                )

                # Existing database meaning:
                #
                # main -> closed
                # tie  -> open
                #
                expected_state = (
                    "closed"
                    if switch_type == "main"
                    else "open"
                )

                baselines["edges"][edge_id] = {
                    "capacity_kw": capacity,
                    "expected_state": expected_state,
                    "switch_type": switch_type,
                    "needs_review": bool(needs_review),
                }

            # ----------------------------------------------------------------
            # DTC
            # ----------------------------------------------------------------

            cur.execute(
                """
                SELECT
                    dtc_id,
                    capacity_kw
                FROM dtc
                """
            )

            for dtc_id, capacity_kw in cur.fetchall():

                if dtc_id is None:
                    continue

                dtc_id = str(dtc_id)

                capacity = (
                    float(capacity_kw)
                    if capacity_kw is not None
                    else 0.0
                )

                baselines["dtc"][dtc_id] = {
                    "capacity_kw": capacity,
                }

            # ----------------------------------------------------------------
            # FACILITIES
            # ----------------------------------------------------------------

            cur.execute(
                """
                SELECT
                    facility_id,
                    facility_type,
                    backup_power_kw
                FROM facilities
                """
            )

            for (
                facility_id,
                facility_type,
                backup_power_kw,
            ) in cur.fetchall():

                if facility_id is None:
                    continue

                facility_id = str(facility_id)

                # backup_power_kw may be NULL because your schema explicitly
                # says it is not available in the source dataset.

                backup_power = (
                    float(backup_power_kw)
                    if backup_power_kw is not None
                    else 0.0
                )

                baselines["facilities"][facility_id] = {
                    "facility_type": facility_type,
                    "typical_load_kw": backup_power * 1.4,
                }

            # ----------------------------------------------------------------
            # SUBSTATIONS
            # ----------------------------------------------------------------

            cur.execute(
                """
                SELECT
                    s.substation_id,
                    COALESCE(
                        SUM(d.capacity_kw),
                        0
                    ) AS capacity_kw
                FROM substations s
                LEFT JOIN dtc d
                    ON d.parent_substation_id = s.substation_id
                GROUP BY s.substation_id
                """
            )

            for (
                substation_id,
                capacity_kw,
            ) in cur.fetchall():

                if substation_id is None:
                    continue

                substation_id = str(substation_id)

                capacity = (
                    float(capacity_kw)
                    if capacity_kw is not None
                    else 0.0
                )

                baselines["substations"][substation_id] = {
                    "capacity_kw": capacity,
                }

    finally:
        conn.close()

    return baselines


# ============================================================================
# REDIS STALE EDGE CLEANUP
# ============================================================================

def remove_stale_edge_keys(valid_edges):
    """
    Remove Redis edge:* keys that do not exist in PostgreSQL.

    This is the important fix for the:

        Live Redis edges found: 42

    vs

        PostgreSQL edges: 31

    problem.

    PostgreSQL is considered authoritative.
    """

    valid_edge_ids = {
        str(edge_id)
        for edge_id in valid_edges.keys()
    }

    removed = 0

    # scan_iter is preferred over KEYS because it is safer for Redis.
    for key in r.scan_iter(match="edge:*"):

        edge_id = key.split(
            ":",
            1,
        )[1]

        if edge_id not in valid_edge_ids:

            r.delete(key)

            removed += 1

            logger.warning(
                "Removed stale Redis edge telemetry: %s",
                key,
            )

    logger.info(
        "Edge cleanup complete. "
        "Valid PostgreSQL edges: %d | "
        "Stale Redis edges removed: %d",
        len(valid_edge_ids),
        removed,
    )


# ============================================================================
# REDIS STALE NODE CLEANUP
# ============================================================================

def remove_stale_node_keys(baselines):
    """
    Remove node:* keys that do not correspond to the current
    PostgreSQL DTC, facility, or substation records.

    This prevents old datasets from remaining in Redis.
    """

    valid_node_ids = set()

    valid_node_ids.update(
        str(x)
        for x in baselines["dtc"].keys()
    )

    valid_node_ids.update(
        str(x)
        for x in baselines["facilities"].keys()
    )

    valid_node_ids.update(
        str(x)
        for x in baselines["substations"].keys()
    )

    removed = 0

    for key in r.scan_iter(match="node:*"):

        node_id = key.split(
            ":",
            1,
        )[1]

        if node_id not in valid_node_ids:

            r.delete(key)

            removed += 1

            logger.warning(
                "Removed stale Redis node telemetry: %s",
                key,
            )

    logger.info(
        "Node cleanup complete. "
        "Valid nodes: %d | "
        "Stale nodes removed: %d",
        len(valid_node_ids),
        removed,
    )


# ============================================================================
# NUMERICAL HELPER
# ============================================================================

def nudge(
    value: float,
    pct: float = 0.03,
) -> float:
    """
    Add a small random variation.

    Example:

        100 kW
        -> approximately 97-103 kW
    """

    if value <= 0:
        return 0.0

    return round(
        value * (
            1 + random.uniform(
                -pct,
                pct,
            )
        ),
        2,
    )


# ============================================================================
# UPDATE HT EDGES
# ============================================================================

def update_edges(edges):
    """
    Generate live readings for ONLY the edges loaded from PostgreSQL.
    """

    updated = 0

    for edge_id, base in edges.items():

        # IMPORTANT:
        # Every key created here originates from PostgreSQL.
        key = f"edge:{edge_id}"

        # --------------------------------------------------------------
        # REAL REVIEW / ANOMALY CONDITION
        # --------------------------------------------------------------

        if base["needs_review"]:

            reading = {
                "current_flow_kw": 0,
                "switch_state": "open",
                "protective_relay_status": "PICKUP",
            }

        # --------------------------------------------------------------
        # NORMAL CONDITION
        # --------------------------------------------------------------

        else:

            if base["expected_state"] == "closed":

                baseline_flow = (
                    base["capacity_kw"] * 0.60
                )

                current_flow = nudge(
                    baseline_flow
                )

            else:

                current_flow = 0

            reading = {
                "current_flow_kw": current_flow,
                "switch_state": base["expected_state"],
                "protective_relay_status": "NORMAL",
            }

        r.hset(
            key,
            mapping=reading,
        )

        updated += 1

    logger.info(
        "Updated %d HT edge telemetry records.",
        updated,
    )


# ============================================================================
# UPDATE DTC
# ============================================================================

def update_dtc(dtc):
    """
    Generate live DTC loading values.
    """

    updated = 0

    for dtc_id, base in dtc.items():

        capacity = base["capacity_kw"]

        if capacity > 0:

            load = nudge(
                capacity * random.uniform(
                    0.50,
                    0.80,
                )
            )

            loading_percentage = round(
                (load / capacity) * 100,
                1,
            )

        else:

            load = 0
            loading_percentage = 0

        r.hset(
            f"node:{dtc_id}",
            mapping={
                "current_load_kw": load,
                "load_kw": load,
                "capacity_kw": capacity,
                "loading_percentage": loading_percentage,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "status": "good",
            },
        )

        updated += 1

    logger.info(
        "Updated %d DTC telemetry records.",
        updated,
    )


# ============================================================================
# UPDATE FACILITIES
# ============================================================================

def update_facilities(facilities):
    """
    Generate live facility load values.
    """

    updated = 0

    for facility_id, base in facilities.items():

        typical_load = base["typical_load_kw"]

        if typical_load > 0:

            load = nudge(
                typical_load * random.uniform(
                    0.60,
                    0.90,
                )
            )

        else:

            # If backup power is NULL / unavailable,
            # keep the live value at 0 rather than inventing
            # a capacity from nowhere.

            load = 0

        r.hset(
            f"node:{facility_id}",
            mapping={
                "current_load_kw": load,
                "avg_utilised_power_1min": nudge(
                    load,
                    pct=0.01,
                ),
                "status": "good",
            },
        )

        updated += 1

    logger.info(
        "Updated %d facility telemetry records.",
        updated,
    )


# ============================================================================
# UPDATE SUBSTATIONS
# ============================================================================

def update_substations(substations):
    """
    Generate live substation load values.

    Substation capacity is derived from the sum of DTC capacities.
    """

    updated = 0

    for substation_id, base in substations.items():

        capacity = base["capacity_kw"]

        if capacity > 0:

            load = nudge(
                capacity * random.uniform(
                    0.45,
                    0.75,
                )
            )

        else:

            load = 0

        r.hset(
            f"node:{substation_id}",
            mapping={
                "current_load_kw": load,
                "status": "good",
            },
        )

        updated += 1

    logger.info(
        "Updated %d substation telemetry records.",
        updated,
    )


# ============================================================================
# RUN ONE TELEMETRY CYCLE
# ============================================================================

def run_cycle(baselines):
    """
    Execute one complete telemetry cycle.

    Cleanup happens BEFORE writing new telemetry.
    """

    logger.info(
        "Running live telemetry cycle..."
    )

    # --------------------------------------------------------------
    # STEP 1
    # Remove stale Redis data.
    # --------------------------------------------------------------

    remove_stale_edge_keys(
        baselines["edges"]
    )

    remove_stale_node_keys(
        baselines
    )

    # --------------------------------------------------------------
    # STEP 2
    # Write current PostgreSQL-backed telemetry.
    # --------------------------------------------------------------

    update_edges(
        baselines["edges"]
    )

    update_dtc(
        baselines["dtc"]
    )

    update_facilities(
        baselines["facilities"]
    )

    update_substations(
        baselines["substations"]
    )

    logger.info(
        "Live telemetry cycle complete."
    )


# ============================================================================
# MAIN
# ============================================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "GRIDBANDHU live telemetry generator"
        )
    )

    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=30,
        help=(
            "Seconds between telemetry cycles "
            "(default: 30)"
        ),
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------------
    # Show configuration
    # ------------------------------------------------------------------------

    logger.info(
        "Using environment file: %s",
        ENV_FILE,
    )

    logger.info(
        "PostgreSQL: %s:%s/%s",
        POSTGRES_HOST,
        POSTGRES_PORT,
        POSTGRES_DB,
    )

    logger.info(
        "Redis: %s:%s / DB %s",
        REDIS_HOST,
        REDIS_PORT,
        REDIS_DB,
    )

    # ------------------------------------------------------------------------
    # Test connections
    # ------------------------------------------------------------------------

    test_connections()

    # ------------------------------------------------------------------------
    # Load PostgreSQL structure
    # ------------------------------------------------------------------------

    baselines = load_baselines()

    logger.info(
        "Loaded real baselines from PostgreSQL: "
        "%d edges, %d DTCs, %d facilities, %d substations.",
        len(baselines["edges"]),
        len(baselines["dtc"]),
        len(baselines["facilities"]),
        len(baselines["substations"]),
    )

    # ------------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------------

    scheduler = BlockingScheduler()

    scheduler.add_job(
        run_cycle,
        "interval",
        seconds=args.interval_seconds,
        args=[baselines],
        id="gridbandhu_live_telemetry",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # ------------------------------------------------------------------------
    # Run immediately once.
    # ------------------------------------------------------------------------

    run_cycle(
        baselines
    )

    logger.info(
        "Live telemetry generator started."
    )

    logger.info(
        "Next cycles every %d seconds.",
        args.interval_seconds,
    )

    try:

        scheduler.start()

    except (KeyboardInterrupt, SystemExit):

        logger.info(
            "Live telemetry generator stopped."
        )

        scheduler.shutdown(
            wait=False
        )


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    main()

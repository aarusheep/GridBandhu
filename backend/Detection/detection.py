"""
trigger_detection.py

GRIDBANDHU — Detection Layer

Purpose:
    Compare live telemetry from Redis against structural rules
    stored in PostgreSQL.

PostgreSQL:
    Database: gridbandhu

    Tables used:
        ht_edges
        dtc

Redis:
    Live telemetry:
        edge:{edge_id}

    Fault state:
        faults:active
        fault:{fault_id}

Fault checks:
    1. Full outage
    2. Overload
    3. Protection trip

Important:
    This script does NOT generate telemetry.

    generate_live_telemetry.py
            |
            v
          Redis
            |
            v
    trigger_detection.py
            |
            v
      Fault state in Redis

Configuration is loaded from .env.
"""

import logging
import os
import time
from pathlib import Path

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
import redis

from dotenv import load_dotenv


# ============================================================================
# LOAD .ENV
# ============================================================================

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_FILE)


# ============================================================================
# CONFIGURATION
# ============================================================================

POLL_INTERVAL_SECONDS = 60


# ----------------------------------------------------------------------------
# PostgreSQL
# ----------------------------------------------------------------------------

POSTGRES_HOST = os.getenv(
    "POSTGRES_HOST",
    "localhost"
)

POSTGRES_PORT = int(
    os.getenv(
        "POSTGRES_PORT",
        "5432"
    )
)

POSTGRES_DB = os.getenv(
    "POSTGRES_DB",
    "gridbandhu"
)

POSTGRES_USER = os.getenv(
    "POSTGRES_USER",
    "postgres"
)

POSTGRES_PASSWORD = os.getenv(
    "POSTGRES_PASSWORD",
    ""
)


# ----------------------------------------------------------------------------
# Redis
# ----------------------------------------------------------------------------

REDIS_HOST = os.getenv(
    "REDIS_HOST",
    "localhost"
)

REDIS_PORT = int(
    os.getenv(
        "REDIS_PORT",
        "6379"
    )
)

REDIS_DB = int(
    os.getenv(
        "REDIS_DB",
        "0"
    )
)


# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

log = logging.getLogger(
    "gridbandhu_detection"
)


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class StructuralRule:
    """
    Structural information about one HT edge.

    This information comes from PostgreSQL.
    """

    edge_id: str

    capacity_kw: Optional[float]

    is_switchable: Optional[bool]

    switch_type: Optional[str]

    needs_review: bool

    substation_id: Optional[str]

    feeder_id: Optional[str]

    parent_dtc_id: Optional[str] = None


@dataclass
class LiveReading:
    """
    Live telemetry for one HT edge.

    This information comes from Redis.
    """

    edge_id: str

    current_flow_kw: Optional[float]

    switch_state: Optional[str]

    relay_status: Optional[str]

    raw: dict


# ============================================================================
# POSTGRESQL CONNECTION
# ============================================================================

def connect_postgres():
    """
    Connect to PostgreSQL using .env values.
    """

    log.info(
        "Connecting to PostgreSQL: %s:%s/%s",
        POSTGRES_HOST,
        POSTGRES_PORT,
        POSTGRES_DB
    )

    return psycopg2.connect(
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
        database=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD
    )


# ============================================================================
# REDIS CONNECTION
# ============================================================================

def connect_redis():
    """
    Connect to Redis using .env values.
    """

    log.info(
        "Connecting to Redis: %s:%s DB=%s",
        REDIS_HOST,
        REDIS_PORT,
        REDIS_DB
    )

    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        db=REDIS_DB,
        decode_responses=True,
        protocol=2
    )


# ============================================================================
# CONNECTION TESTS
# ============================================================================

def test_postgres_connection(pg_conn):
    """
    Verify PostgreSQL connection and show current database/user.
    """

    with pg_conn.cursor() as cur:

        cur.execute(
            """
            SELECT current_database(), current_user;
            """
        )

        database, user = cur.fetchone()

        log.info(
            "PostgreSQL connected successfully."
        )

        log.info(
            "Database: %s | User: %s",
            database,
            user
        )


def test_redis_connection(redis_conn):
    """
    Verify Redis connection.
    """

    if redis_conn.ping():

        log.info(
            "Redis connected successfully."
        )


# ============================================================================
# EXPECTED SWITCH STATE
# ============================================================================

def expected_switch_state(
    rule: StructuralRule
) -> Optional[str]:
    """
    Determine the expected normal switch state.

    Current GridBandhu assumption:

        main -> CLOSED
        tie  -> OPEN

    Non-switchable edges:
        no expected switch state
    """

    if not rule.is_switchable:

        return None

    if not rule.switch_type:

        return None

    switch_type = rule.switch_type.strip().upper()

    if switch_type == "MAIN":

        return "CLOSED"

    if switch_type == "TIE":

        return "OPEN"

    return None


# ============================================================================
# LOAD STRUCTURAL RULES FROM POSTGRESQL
# ============================================================================

def load_structural_rules(
    pg_conn
) -> dict[str, StructuralRule]:
    """
    Load structural information from the actual GridBandhu schema.

    Tables:

        ht_edges
            |
            +---- dtc.parent_ht_edge_id

    PostgreSQL is the source of truth for fixed structural information.
    """

    rules: dict[str, StructuralRule] = {}

    query = """
        SELECT
            h.edge_id,
            h.capacity_kw,
            h.is_switchable,
            h.switch_type,
            h.needs_review,
            h.substation_id,
            h.feeder_id,
            d.dtc_id AS parent_dtc_id

        FROM ht_edges h

        LEFT JOIN dtc d
            ON d.parent_ht_edge_id = h.edge_id

        ORDER BY h.edge_id;
    """

    with pg_conn.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor
    ) as cur:

        cur.execute(query)

        rows = cur.fetchall()

        for row in rows:

            edge_id = row["edge_id"]

            capacity = row["capacity_kw"]

            if capacity is not None:

                capacity = float(capacity)

            rules[edge_id] = StructuralRule(
                edge_id=edge_id,
                capacity_kw=capacity,
                is_switchable=row["is_switchable"],
                switch_type=row["switch_type"],
                needs_review=bool(
                    row["needs_review"]
                ),
                substation_id=row["substation_id"],
                feeder_id=row["feeder_id"],
                parent_dtc_id=row["parent_dtc_id"]
            )

    log.info(
        "Loaded structural rules for %d HT edges.",
        len(rules)
    )

    return rules


# ============================================================================
# SAFE FLOAT CONVERSION
# ============================================================================

def safe_float(
    value
) -> Optional[float]:
    """
    Safely convert a Redis value to float.
    """

    try:

        return float(value)

    except (
        TypeError,
        ValueError
    ):

        return None


# ============================================================================
# READ LIVE TELEMETRY FROM REDIS
# ============================================================================

def get_live_readings(
    redis_conn
) -> list[LiveReading]:
    """
    Read all live HT-edge telemetry from Redis.

    Expected Redis keys:

        edge:{edge_id}

    Expected fields:

        current_flow_kw
        switch_state
        relay_status

    These names match generate_live_telemetry.py.
    """

    readings: list[LiveReading] = []

    for key in redis_conn.scan_iter(
        match="edge:*"
    ):

        edge_id = key.split(
            ":",
            1
        )[1]

        raw = redis_conn.hgetall(
            key
        )

        if not raw:

            log.warning(
                "Edge %s has an empty Redis record. "
                "Skipping.",
                edge_id
            )

            continue

        # ------------------------------------------------------------
        # Current flow
        # ------------------------------------------------------------

        flow = safe_float(
            raw.get(
                "current_flow_kw"
            )
        )

        if flow is None:

            log.warning(
                "Edge %s: current_flow_kw is missing "
                "or non-numeric: %r. Skipping.",
                edge_id,
                raw.get(
                    "current_flow_kw"
                )
            )

            continue

        # ------------------------------------------------------------
        # IMPORTANT:
        #
        # The current ingestion layer publishes protective_relay_status;
        # accept the detector's documented relay_status name as well.
        # ------------------------------------------------------------

        relay_status = raw.get("relay_status") or raw.get("protective_relay_status")

        readings.append(
            LiveReading(
                edge_id=edge_id,
                current_flow_kw=flow,
                switch_state=raw.get(
                    "switch_state"
                ),
                relay_status=relay_status,
                raw=raw
            )
        )

    return readings


# ============================================================================
# FAULT CHECK 1 — FULL OUTAGE
# ============================================================================

def check_full_outage(
    reading: LiveReading,
    rule: StructuralRule
) -> bool:
    """
    Full outage occurs when:

        expected switch state = CLOSED
        AND
        actual switch state = CLOSED
        AND
        current flow = 0
    """

    if reading.switch_state is None:

        return False

    expected_state = expected_switch_state(
        rule
    )

    if expected_state != "CLOSED":

        return False

    actual_state = (
        reading.switch_state
        .strip()
        .upper()
    )

    return (
        actual_state == "CLOSED"
        and reading.current_flow_kw == 0
    )


# ============================================================================
# FAULT CHECK 2 — OVERLOAD
# ============================================================================

def check_overload(
    reading: LiveReading,
    rule: StructuralRule
) -> bool:
    """
    Overload occurs when:

        current flow > structural capacity
    """

    if reading.current_flow_kw is None:

        return False

    if rule.capacity_kw is None:

        log.warning(
            "Edge %s has no capacity_kw in PostgreSQL.",
            rule.edge_id
        )

        return False

    return (
        reading.current_flow_kw
        > rule.capacity_kw
    )


# ============================================================================
# FAULT CHECK 3 — PROTECTION TRIP
# ============================================================================

def check_protection_trip(
    reading: LiveReading,
    rule: StructuralRule
) -> bool:
    """
    Protection fault occurs when:

    1. Relay reports a fault condition.

       Recognized statuses:

           TRIPPED
           FAULT
           ALARM
           PICKUP

    OR

    2. A switch that should normally be CLOSED
       is currently OPEN.

    Tie lines are normally OPEN, so an OPEN tie
    does NOT automatically trigger this condition.
    """

    # ----------------------------------------------------------------
    # Relay status
    # ----------------------------------------------------------------

    if reading.relay_status:

        relay_status = (
            reading.relay_status
            .strip()
            .upper()
        )

        if relay_status in (
            "TRIPPED",
            "FAULT",
            "ALARM",
            "PICKUP"
        ):

            return True

    # ----------------------------------------------------------------
    # Unexpected switch opening
    # ----------------------------------------------------------------

    expected_state = expected_switch_state(
        rule
    )

    if (
        expected_state == "CLOSED"
        and reading.switch_state is not None
        and reading.switch_state.strip().upper()
        == "OPEN"
    ):

        return True

    return False


# ============================================================================
# EVALUATE ONE EDGE
# ============================================================================

def evaluate_edge(
    reading: LiveReading,
    rule: StructuralRule
) -> Optional[str]:
    """
    Run all fault checks.

    Returns:

        full_outage
        overload
        protection_trip
        None
    """

    # ------------------------------------------------------------
    # 1. Full outage
    # ------------------------------------------------------------

    if check_full_outage(
        reading,
        rule
    ):

        return "full_outage"

    # ------------------------------------------------------------
    # 2. Overload
    # ------------------------------------------------------------

    if check_overload(
        reading,
        rule
    ):

        return "overload"

    # ------------------------------------------------------------
    # 3. Protection trip
    # ------------------------------------------------------------

    if check_protection_trip(
        reading,
        rule
    ):

        return "protection_trip"

    return None


# ============================================================================
# BUILD FAULT PATH
# ============================================================================

def build_path_description(
    rule: StructuralRule
) -> str:
    """
    Build a simple topology path.

    Current available relationship:

        Substation
             |
          HT Edge
             |
           DTC
    """

    parts = []

    if rule.substation_id:

        parts.append(
            rule.substation_id
        )

    parts.append(
        rule.edge_id
    )

    if rule.parent_dtc_id:

        parts.append(
            rule.parent_dtc_id
        )

    return " → ".join(
        parts
    )


# ============================================================================
# RAISE FAULT IN REDIS
# ============================================================================

def raise_fault(
    redis_conn,
    edge_id: str,
    fault_type: str,
    rule: StructuralRule
):
    """
    Create/update an active fault record.
    """

    fault_id = (
        f"fault_{edge_id}"
    )

    detected_at = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    path_description = (
        build_path_description(
            rule
        )
    )

    pipe = (
        redis_conn.pipeline()
    )

    # ------------------------------------------------------------
    # Mark edge as faulted
    # ------------------------------------------------------------

    pipe.hset(
        f"edge:{edge_id}",
        mapping={
            "is_faulted": "true"
        }
    )

    # ------------------------------------------------------------
    # Add fault to active set
    # ------------------------------------------------------------

    pipe.sadd(
        "faults:active",
        fault_id
    )

    # ------------------------------------------------------------
    # Store fault details
    # ------------------------------------------------------------

    pipe.hset(
        f"fault:{fault_id}",
        mapping={
            "edge_id": edge_id,
            "fault_type": fault_type,
            "path_description": path_description,
            "detected_at": detected_at,
            "substation_id": (
                rule.substation_id
                or ""
            ),
            "feeder_id": (
                rule.feeder_id
                or ""
            ),
            "dtc_id": (
                rule.parent_dtc_id
                or ""
            )
        }
    )

    pipe.execute()

    log.warning(
        "🚨 FAULT [%s] on %s — %s",
        fault_type,
        edge_id,
        path_description
    )


# ============================================================================
# CLEAR FAULT
# ============================================================================

def clear_fault(
    redis_conn,
    edge_id: str
):
    """
    Remove an edge from the active fault state.
    """

    fault_id = (
        f"fault_{edge_id}"
    )

    pipe = (
        redis_conn.pipeline()
    )

    # Mark edge normal

    pipe.hset(
        f"edge:{edge_id}",
        mapping={
            "is_faulted": "false"
        }
    )

    # Remove from active set

    pipe.srem(
        "faults:active",
        fault_id
    )

    # Delete fault details

    pipe.delete(
        f"fault:{fault_id}"
    )

    pipe.execute()

    log.info(
        "✅ RESOLVED: %s",
        edge_id
    )


# ============================================================================
# CHECK EXISTING FAULT STATE
# ============================================================================

def is_currently_marked_faulted(
    redis_conn,
    edge_id: str
) -> bool:
    """
    Check whether Redis currently marks this edge as faulted.
    """

    value = redis_conn.hget(
        f"edge:{edge_id}",
        "is_faulted"
    )

    return (
        str(value).lower()
        == "true"
    )


# ============================================================================
# ONE DETECTION CYCLE
# ============================================================================

def run_cycle(
    pg_conn,
    redis_conn,
    rules: dict[str, StructuralRule]
):
    """
    Run one complete detection cycle.
    """

    print()
    print("=" * 70)
    print("RUNNING DETECTION CYCLE")
    print("=" * 70)

    # ------------------------------------------------------------
    # Get current telemetry
    # ------------------------------------------------------------

    readings = get_live_readings(
        redis_conn
    )

    print(
        f"Live Redis edges found: "
        f"{len(readings)}"
    )

    checked = 0

    newly_faulted = []

    still_faulted = []

    newly_cleared = []

    # ------------------------------------------------------------
    # Evaluate every live edge
    # ------------------------------------------------------------

    for reading in readings:

        rule = rules.get(
            reading.edge_id
        )

        # --------------------------------------------------------
        # Redis edge does not exist in PostgreSQL
        # --------------------------------------------------------

        if rule is None:

            log.warning(
                "Edge %s exists in Redis but "
                "does not exist in PostgreSQL ht_edges. "
                "Skipping.",
                reading.edge_id
            )

            continue

        checked += 1

        # --------------------------------------------------------
        # Evaluate
        # --------------------------------------------------------

        fault_type = evaluate_edge(
            reading,
            rule
        )

        currently_faulted = (
            is_currently_marked_faulted(
                redis_conn,
                reading.edge_id
            )
        )

        # ========================================================
        # FAULT
        # ========================================================

        if fault_type:

            raise_fault(
                redis_conn,
                reading.edge_id,
                fault_type,
                rule
            )

            if currently_faulted:

                still_faulted.append(
                    (
                        reading.edge_id,
                        fault_type
                    )
                )

            else:

                newly_faulted.append(
                    (
                        reading.edge_id,
                        fault_type
                    )
                )

        # ========================================================
        # NORMAL AGAIN
        # ========================================================

        elif currently_faulted:

            clear_fault(
                redis_conn,
                reading.edge_id
            )

            newly_cleared.append(
                reading.edge_id
            )

    # =========================================================================
    # SUMMARY
    # =========================================================================

    print()

    # ------------------------------------------------------------
    # Newly detected
    # ------------------------------------------------------------

    for edge_id, fault_type in newly_faulted:

        print(
            f"🚨 FAULT DETECTED: "
            f"{edge_id} "
            f"({fault_type})"
        )

    # ------------------------------------------------------------
    # Still active
    # ------------------------------------------------------------

    for edge_id, fault_type in still_faulted:

        print(
            f"⚠️ STILL ACTIVE: "
            f"{edge_id} "
            f"({fault_type})"
        )

    # ------------------------------------------------------------
    # Resolved
    # ------------------------------------------------------------

    for edge_id in newly_cleared:

        print(
            f"✅ RESOLVED: "
            f"{edge_id}"
        )

    # ------------------------------------------------------------
    # Active fault set
    # ------------------------------------------------------------

    active_faults = (
        redis_conn.smembers(
            "faults:active"
        )
    )

    if active_faults:

        print()
        print(
            f"⚠️ Active faults: "
            f"{len(active_faults)}"
        )

        for fault_id in sorted(
            active_faults
        ):

            print(
                f"   • {fault_id}"
            )

    else:

        print()
        print(
            "✅ No active faults — grid nominal."
        )

    print()
    print(
        f"Edges checked: {checked}"
    )

    print(
        "=" * 70
    )


# ============================================================================
# STARTUP CHECK
# ============================================================================

def run_startup_check(
    pg_conn,
    redis_conn,
    rules
):
    """
    Check immediately when the detector starts.

    This prevents waiting 60 seconds before detecting
    an already-existing fault.
    """

    log.info(
        "Running startup detection check..."
    )

    run_cycle(
        pg_conn,
        redis_conn,
        rules
    )

    log.info(
        "Startup detection check complete."
    )


# ============================================================================
# MAIN
# ============================================================================

def main():

    pg_conn = None

    redis_conn = None

    try:

        # ==========================================================
        # PostgreSQL
        # ==========================================================

        pg_conn = connect_postgres()

        test_postgres_connection(
            pg_conn
        )

        # ==========================================================
        # Redis
        # ==========================================================

        redis_conn = connect_redis()

        test_redis_connection(
            redis_conn
        )

        # ==========================================================
        # Load PostgreSQL structural rules
        # ==========================================================

        rules = load_structural_rules(
            pg_conn
        )

        if not rules:

            log.warning(
                "No records found in ht_edges."
            )

        # ==========================================================
        # Initial check
        # ==========================================================

        run_startup_check(
            pg_conn,
            redis_conn,
            rules
        )

        # ==========================================================
        # Continuous loop
        # ==========================================================

        log.info(
            "Detection layer is running."
        )

        log.info(
            "Detection interval: %d seconds",
            POLL_INTERVAL_SECONDS
        )

        while True:

            cycle_start = (
                time.monotonic()
            )

            try:

                # --------------------------------------------------
                # Reload structural data
                # --------------------------------------------------

                rules = (
                    load_structural_rules(
                        pg_conn
                    )
                )

                # --------------------------------------------------
                # Detect faults
                # --------------------------------------------------

                run_cycle(
                    pg_conn,
                    redis_conn,
                    rules
                )
                # Let the API distinguish a current detector result from a
                # stale faults:active record after the detector is stopped.
                redis_conn.set("detection:heartbeat", datetime.now(timezone.utc).isoformat(), ex=max(POLL_INTERVAL_SECONDS * 2 + 30, 90))

            # ======================================================
            # PostgreSQL connection failure
            # ======================================================

            except psycopg2.OperationalError as e:

                log.error(
                    "PostgreSQL connection error: %s",
                    e
                )

                try:

                    pg_conn.close()

                except Exception:

                    pass

                log.info(
                    "Attempting PostgreSQL reconnect..."
                )

                time.sleep(5)

                try:

                    pg_conn = (
                        connect_postgres()
                    )

                    test_postgres_connection(
                        pg_conn
                    )

                except Exception as reconnect_error:

                    log.error(
                        "PostgreSQL reconnect failed: %s",
                        reconnect_error
                    )

            # ======================================================
            # Redis connection failure
            # ======================================================

            except redis.exceptions.ConnectionError as e:

                log.error(
                    "Redis connection error: %s",
                    e
                )

                try:

                    redis_conn.close()

                except Exception:

                    pass

                log.info(
                    "Attempting Redis reconnect..."
                )

                time.sleep(5)

                try:

                    redis_conn = (
                        connect_redis()
                    )

                    test_redis_connection(
                        redis_conn
                    )

                except Exception as reconnect_error:

                    log.error(
                        "Redis reconnect failed: %s",
                        reconnect_error
                    )

            # ======================================================
            # Any unexpected error
            # ======================================================

            except Exception as e:

                log.exception(
                    "Unexpected error during detection cycle: %s",
                    e
                )

            # ======================================================
            # Maintain 60-second interval
            # ======================================================

            elapsed = (
                time.monotonic()
                - cycle_start
            )

            sleep_for = max(
                0,
                POLL_INTERVAL_SECONDS
                - elapsed
            )

            time.sleep(
                sleep_for
            )

    # =========================================================================
    # CTRL+C
    # =========================================================================

    except KeyboardInterrupt:

        log.info(
            "Detection layer stopped by user."
        )

    # =========================================================================
    # FATAL ERROR
    # =========================================================================

    except Exception as e:

        log.exception(
            "Fatal error: %s",
            e
        )

    # =========================================================================
    # CLEANUP
    # =========================================================================

    finally:

        if pg_conn:

            try:
                pg_conn.close()

            except Exception:

                pass

        if redis_conn:

            try:
                redis_conn.close()

            except Exception:

                pass

        log.info(
            "Database connections closed."
        )


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":

    main()

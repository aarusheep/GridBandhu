"""
GridBandhu — Redis + PostGIS State Bridge for MILP
===================================================
Reads LIVE telemetry from Redis (edge:*, node:* hashes)
and STRUCTURAL data from PostGIS (capacity, feeder groupings, facility types)
to produce the feeder-level dicts the MILP solver expects.

Redis keys consumed:
    edge:{edge_id}    → current_flow_kw, switch_state, protective_relay_status
    node:{dtc_id}     → current_load_kw, loading_percentage
    node:{fac_id}     → current_load_kw, avg_utilised_power_1min
    node:{sub_id}     → current_load_kw
    faults:active     → set of currently faulted edge IDs

Redis keys written:
    milp:allocation          → latest solver result (JSON)
    milp:allocation:previous → previous result (for diff)
    milp:paths:{fault_id}    → top 3 reconfiguration paths (JSON)
"""

import os
import json
import redis
import psycopg2
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

_redis = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    db=int(os.getenv("REDIS_DB", 0)),
    decode_responses=True,
)


def _get_pg():
    try:
        return psycopg2.connect(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=os.getenv("POSTGRES_PORT", 5432),
            dbname=os.getenv("POSTGRES_DB", "gridbandhu"),
            user=os.getenv("POSTGRES_USER", "postgres"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# CRITICALITY + PRIORITY WEIGHTS
# ---------------------------------------------------------------------------

CRITICALITY_WEIGHTS = {
    "High": 1.0,      # Hospitals, Water Plants, Fire Stations
    "Medium": 0.5,     # Power utilities
    "Low": 0.3,        # Commercial, Residential
}

FACILITY_TYPE_WEIGHTS = {
    "HOSPITAL": 0.9,
    "WATER_UTILITY": 1.0,
    "FIRE_STATION": 0.85,
    "POWER_UTILITY": 0.5,
    "POWER_PLANT": 0.5,
    "COMMERCIAL": 0.3,
    "RESIDENTIAL": 0.4,
}

MIN_SAFE_FRACTIONS = {
    "HOSPITAL": 0.40,
    "WATER_UTILITY": 0.30,
    "FIRE_STATION": 0.25,
    "POWER_UTILITY": 0.0,
    "POWER_PLANT": 0.0,
    "COMMERCIAL": 0.0,
    "RESIDENTIAL": 0.0,
}

EVENT_SEVERITY = {
    "Normal": 1.0,
    "High_Demand": 1.2,
    "Grid_Stress": 1.5,
    "Disaster_Stress": 2.0,
}


# ---------------------------------------------------------------------------
# READ — Structural data from PostGIS (cached, called once or rarely)
# ---------------------------------------------------------------------------

_structural_cache = None


def load_structural_data() -> dict:
    """Load structural baseline from PostGIS. Cached after first call."""
    global _structural_cache
    if _structural_cache is not None:
        return _structural_cache

    conn = _get_pg()
    if conn is None:
        print("[schema] WARNING: PostGIS not available, using empty structure")
        _structural_cache = {"feeders": {}, "substations": {}, "edges": {}, "facilities": {}}
        return _structural_cache

    cur = conn.cursor()

    # Edges grouped by feeder
    cur.execute("""
        SELECT edge_id, feeder_id, capacity_kw, switch_type, needs_review, substation_id
        FROM ht_edges
    """)
    edges = {}
    feeders = {}
    for edge_id, feeder_id, capacity_kw, switch_type, needs_review, sub_id in cur.fetchall():
        edges[edge_id] = {
            "feeder_id": feeder_id,
            "capacity_kw": float(capacity_kw),
            "switch_type": switch_type,
            "needs_review": bool(needs_review),
            "substation_id": sub_id,
        }
        if feeder_id not in feeders:
            feeders[feeder_id] = {
                "feeder_id": feeder_id,
                "substation_id": sub_id,
                "edge_ids": [],
                "total_capacity_kw": 0,
                "facility_types": [],
                "criticality": "Low",
                "priority_weight": 0.3,
                "min_safe_fraction": 0.0,
            }
        feeders[feeder_id]["edge_ids"].append(edge_id)
        feeders[feeder_id]["total_capacity_kw"] += float(capacity_kw)

    # Facilities connected via DTCs → map to feeders
    cur.execute("""
        SELECT f.facility_id, f.facility_type, f.criticality_tier,
               f.backup_power_kw, f.connected_dtc_id,
               d.parent_ht_edge_id
        FROM facilities f
        JOIN dtc d ON f.connected_dtc_id = d.dtc_id
    """)
    facilities = {}
    for fac_id, fac_type, crit, backup_kw, dtc_id, ht_edge_id in cur.fetchall():
        facilities[fac_id] = {
            "facility_type": fac_type,
            "criticality": crit,
            "backup_power_kw": float(backup_kw) if backup_kw else 0,
            "dtc_id": dtc_id,
            "ht_edge_id": ht_edge_id,
        }
        # Find which feeder this facility belongs to
        if ht_edge_id in edges:
            fid = edges[ht_edge_id]["feeder_id"]
            if fid in feeders:
                feeders[fid]["facility_types"].append(fac_type)
                # Upgrade criticality to highest facility on this feeder
                fac_weight = FACILITY_TYPE_WEIGHTS.get(str(fac_type).upper(), 0.3)
                if fac_weight > feeders[fid]["priority_weight"]:
                    feeders[fid]["priority_weight"] = fac_weight
                    feeders[fid]["criticality"] = crit or "Low"
                fac_safe = MIN_SAFE_FRACTIONS.get(str(fac_type).upper(), 0.0)
                if fac_safe > feeders[fid]["min_safe_fraction"]:
                    feeders[fid]["min_safe_fraction"] = fac_safe

    # Substations
    cur.execute("""
        SELECT s.substation_id, s.name, COALESCE(SUM(d.capacity_kw), 0)
        FROM substations s
        LEFT JOIN dtc d ON d.parent_substation_id = s.substation_id
        GROUP BY s.substation_id, s.name
    """)
    substations = {}
    for sub_id, name, total_cap in cur.fetchall():
        substations[sub_id] = {
            "substation_id": sub_id,
            "name": name,
            "total_capacity_kw": float(total_cap),
        }

    cur.close()
    conn.close()

    _structural_cache = {
        "feeders": feeders,
        "substations": substations,
        "edges": edges,
        "facilities": facilities,
    }
    print(f"[schema] Structural data loaded: {len(feeders)} feeders, "
          f"{len(substations)} substations, {len(edges)} edges, {len(facilities)} facilities")
    return _structural_cache


def refresh_structural_data():
    """Force reload from PostGIS (call if topology changes)."""
    global _structural_cache
    _structural_cache = None
    return load_structural_data()


# ---------------------------------------------------------------------------
# READ — Live telemetry from Redis → aggregate to feeder level for MILP
# ---------------------------------------------------------------------------

def get_all_feeders() -> list[dict]:
    """
    Reads live edge telemetry from Redis, combines with structural data
    from PostGIS, and produces per-feeder dicts ready for the MILP solver.
    """
    structure = load_structural_data()
    feeders_struct = structure["feeders"]
    edges_struct = structure["edges"]

    result = []

    for feeder_id, finfo in feeders_struct.items():
        # Aggregate live data across all edges in this feeder
        total_flow_kw = 0
        worst_relay = "NORMAL"
        any_open = False
        relay_priority = {"NORMAL": 0, "PICKUP": 1, "ALARM": 2, "FAULT": 3, "TRIPPED": 4}

        for edge_id in finfo["edge_ids"]:
            live = _safe_hgetall(f"edge:{edge_id}")
            if not live:
                continue

            flow = float(live.get("current_flow_kw", 0))
            total_flow_kw += flow

            relay = live.get("protective_relay_status", "NORMAL").upper()
            if relay_priority.get(relay, 0) > relay_priority.get(worst_relay, 0):
                worst_relay = relay

            if live.get("switch_state", "closed").lower() == "open":
                any_open = True

        # Convert to MW
        demand_mw = round(total_flow_kw / 1000, 4)
        capacity_mw = round(finfo["total_capacity_kw"] / 1000, 4)

        # Determine load type label
        fac_types = finfo["facility_types"]
        if "HOSPITAL" in fac_types:
            load_type = "Hospital"
        elif "WATER_UTILITY" in fac_types:
            load_type = "Water Plant"
        elif "FIRE_STATION" in fac_types:
            load_type = "Fire Station"
        else:
            load_type = "Industrial"

        result.append({
            "feeder_id": feeder_id,
            "demand_mw": demand_mw,
            "capacity_mw": capacity_mw,
            "priority_weight": finfo["priority_weight"],
            "severity_multiplier": 1.0,  # updated by event condition
            "min_safe_fraction": finfo["min_safe_fraction"],
            "loss_factor": 1.05,  # default, can be refined per feeder
            "load_type": load_type,
            "relay_status": worst_relay,
            "edge_ids": finfo["edge_ids"],
            "facility_types": fac_types,
        })

    return result


def get_total_supply() -> float:
    """
    Total supply from all substations.
    Reads live substation load from Redis, falls back to PostGIS capacity.
    """
    structure = load_structural_data()
    total = 0
    for sub_id, sub_info in structure["substations"].items():
        live = _safe_hgetall(f"node:{sub_id}")
        if live and "current_load_kw" in live:
            total += float(live["current_load_kw"])
        else:
            # Fallback to 70% of structural capacity
            total += sub_info["total_capacity_kw"] * 0.7
    return round(total / 1000, 4)  # kW to MW


def get_substation() -> dict:
    """Get aggregated substation state."""
    return {
        "total_supply_mw": get_total_supply(),
        "event_condition": "Normal",
    }


# ---------------------------------------------------------------------------
# READ — Faults from detection layer
# ---------------------------------------------------------------------------

def get_active_faults() -> list[str]:
    """Returns list of edge IDs currently marked as faulted."""
    try:
        return list(_redis.smembers("faults:active"))
    except Exception:
        return []


def has_new_faults() -> bool:
    """Check if there are any active faults that need MILP attention."""
    try:
        return _redis.scard("faults:active") > 0
    except Exception:
        return False


def get_fault_details(edge_id: str) -> dict:
    """Get fault info from the edge's Redis hash."""
    live = _safe_hgetall(f"edge:{edge_id}")
    return {
        "edge_id": edge_id,
        "fault_type": live.get("fault_type", "unknown"),
        "fault_detail": live.get("fault_detail", ""),
        "detected_at": live.get("fault_detected_at", ""),
        "is_faulted": live.get("is_faulted", "false") == "true",
    }


# ---------------------------------------------------------------------------
# WRITE — MILP results back to Redis
# ---------------------------------------------------------------------------

def store_allocation(result: dict):
    """Store latest MILP allocation. Moves current to previous for diff."""
    try:
        current = _redis.get("milp:allocation")
        if current:
            _redis.set("milp:allocation:previous", current)
        _redis.set("milp:allocation", json.dumps(result))
    except Exception as e:
        print(f"[schema] Failed to store allocation: {e}")


def get_allocation() -> dict | None:
    """Get latest MILP allocation result."""
    try:
        data = _redis.get("milp:allocation")
        return json.loads(data) if data else None
    except Exception:
        return None


def get_previous_allocation() -> dict | None:
    """Get previous allocation for diff."""
    try:
        data = _redis.get("milp:allocation:previous")
        return json.loads(data) if data else None
    except Exception:
        return None


def store_paths(fault_id: str, paths: list[dict]):
    """Store top 3 reconfiguration paths for a specific fault."""
    try:
        _redis.set(f"milp:paths:{fault_id}", json.dumps(paths))
    except Exception as e:
        print(f"[schema] Failed to store paths for {fault_id}: {e}")


def get_paths(fault_id: str) -> list[dict]:
    """Get stored reconfiguration paths for a fault."""
    try:
        data = _redis.get(f"milp:paths:{fault_id}")
        return json.loads(data) if data else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# WRITE — Feeder snapshot (for manual seeding / testing)
# ---------------------------------------------------------------------------

def store_feeder_snapshot(feeder_id: str, snapshot: dict):
    """Store a feeder snapshot as JSON string (for test.py compatibility)."""
    snapshot["feeder_id"] = feeder_id
    _redis.set(f"gridshield:feeder:{feeder_id}", json.dumps(snapshot))


def store_substation_snapshot(snapshot: dict):
    """Store substation snapshot as JSON string (for test.py compatibility)."""
    _redis.set(f"gridshield:substation", json.dumps(snapshot))


# ---------------------------------------------------------------------------
# UTILITY
# ---------------------------------------------------------------------------

def _safe_hgetall(key: str) -> dict:
    """Read a Redis hash, return empty dict on any error."""
    try:
        return _redis.hgetall(key)
    except Exception:
        return {}


def ping() -> bool:
    """Check Redis connectivity."""
    try:
        return _redis.ping()
    except Exception:
        return False


def flush():
    """Clear all MILP keys (not telemetry keys)."""
    try:
        for pattern in ["milp:*", "gridshield:*"]:
            keys = _redis.keys(pattern)
            if keys:
                _redis.delete(*keys)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Quick test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not ping():
        print("Redis not running")
        exit(1)

    print("Redis connected\n")

    # Load structural data from PostGIS
    structure = load_structural_data()
    print()

    # Read live feeders from Redis
    feeders = get_all_feeders()
    supply = get_total_supply()

    print(f"Live feeders from Redis: {len(feeders)}")
    for f in feeders:
        print(f"  {f['feeder_id']}: {f['demand_mw']} MW, "
              f"capacity={f['capacity_mw']} MW, "
              f"priority={f['priority_weight']}, "
              f"type={f['load_type']}, "
              f"relay={f['relay_status']}")
    print(f"\nTotal supply: {supply} MW")

    # Check faults
    faults = get_active_faults()
    print(f"Active faults: {len(faults)}")
    for fid in faults:
        print(f"  {fid}: {get_fault_details(fid)}")
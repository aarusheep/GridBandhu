"""
GridShield -- Solver Scheduler
Fault-driven pattern:
    1. Topology (PostGIS/static) -- loaded once at startup, cached
    2. Redis faults:active -- polled every cycle; the solver only runs
       when there is at least one active fault
    3. Redis edge:*/node:* -- read fresh for live telemetry when solving

Flow per cycle:
    poll faults:active -> if empty, skip
                        -> if non-empty, read topology (cached) + redis
                           (live) -> solver -> result -> redis (write back)
"""

import asyncio
from datetime import datetime
from .solver import solve
from .diff import generate_diff, format_summary
from backend.state.schema import (
    get_all_feeders,
    get_total_supply,
    store_allocation,
    has_new_faults,
)

POLL_INTERVAL_SECONDS = 5  # how often to check faults:active for new work
_cached_topology = None
_previous_allocations = []


def load_topology():
    """
    Load network topology once at startup.
    Replace with actual topology module when ready.

    Expected interface from topology team:
        get_graph() returns dict with:
        {
            "nodes": [...],       # substations, loads
            "edges": [...],       # from_node, to_node, capacity_kw, is_switchable
            "switches": [...]     # switch_id, from_node, to_node, state, switch_type
        }
    """
    global _cached_topology

    try:
        from topology.graph import get_graph
        _cached_topology = get_graph()
        print(f"[Scheduler] Topology loaded: {len(_cached_topology.get('nodes', []))} nodes, "
              f"{len(_cached_topology.get('edges', []))} edges, "
              f"{len(_cached_topology.get('switches', []))} switches")
    except ImportError:
        print("[Scheduler] Topology module not available yet -- running allocation-only mode")
        _cached_topology = None
    except Exception as e:
        print(f"[Scheduler] Topology load failed: {e} -- running allocation-only mode")
        _cached_topology = None

    return _cached_topology


def refresh_topology():
    """Call if network structure changes (rare). Not needed every cycle."""
    return load_topology()


def run_single_cycle() -> dict | None:
    """
    One check-and-solve cycle:
        1. Check faults:active in Redis -- skip entirely if empty
        2. Read live feeder state from Redis + cached topology
        3. Solve the MILP
        4. Write result back to Redis
    """
    global _previous_allocations

    if not has_new_faults():
        return None

    feeders = get_all_feeders()
    supply = get_total_supply()

    if not feeders:
        print("[Scheduler] Fault active but no feeder data in Redis, skipping")
        return None

    result = solve(feeders, supply, topology=_cached_topology)
    result["timestamp"] = datetime.now().isoformat()

    changes = generate_diff(_previous_allocations, result["allocations"])
    result["changes"] = changes
    result["summary"] = format_summary(result, changes)

    store_allocation(result)
    _previous_allocations = result["allocations"]

    print(f"[{result['timestamp']}] {result['summary']} ({result['solve_time_ms']}ms)")
    if changes:
        for c in changes:
            print(f"  -> {c['reason']}")

    return result


async def run_loop():
    """
    Main async loop. Start from FastAPI:
        @app.on_event("startup")
        async def startup():
            load_topology()
            asyncio.create_task(run_loop())
    """
    print(f"[Scheduler] Started -- polling faults:active every {POLL_INTERVAL_SECONDS}s")

    while True:
        run_single_cycle()
        await asyncio.sleep(POLL_INTERVAL_SECONDS)

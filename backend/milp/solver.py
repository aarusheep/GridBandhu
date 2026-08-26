"""
GridBandhu — Solver
Runs the MILP and produces TOP 3 ranked allocation alternatives.
Each alternative uses a different strategy to force diverse solutions.
"""

import time
import pulp
from milp.formulation import build_model
from state.schema import store_paths, get_active_faults, get_fault_details


def solve(feeders: list[dict], total_supply_mw: float, topology: dict = None) -> dict:
    """
    Run the MILP solver and return the BEST allocation.
    Also generates 3 alternative paths and stores them in Redis.
    """
    total_demand = sum(f["demand_mw"] for f in feeders)

    # Solve primary (best) allocation
    start = time.time()
    prob, x, y = build_model(feeders, total_supply_mw, topology)
    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    solve_ms = (time.time() - start) * 1000

    primary = _extract_result(prob, x, y, feeders, total_supply_mw, total_demand, solve_ms)

    # Generate top 3 alternatives and store per fault
    faults = get_active_faults()
    if faults:
        paths = _generate_alternatives(feeders, total_supply_mw, topology, primary)
        for fault_id in faults:
            fault_detail = get_fault_details(fault_id)
            for p in paths:
                p["fault_id"] = fault_id
                p["fault_detail"] = fault_detail.get("fault_detail", "")
            store_paths(fault_id, paths)

    return primary


def _generate_alternatives(feeders, total_supply_mw, topology, primary_result) -> list[dict]:
    """
    Generate 3 ranked alternatives with different strategies:
    
    Path 1: Best weighted allocation (maximize priority * demand * fraction)
    Path 2: Maximum breadth (serve as many feeders as possible, even partially)
    Path 3: Critical only (shed everything non-critical, maximize critical load)
    """
    paths = []

    # --- Path 1: Best overall (primary result) ---
    paths.append({
        "path_id": 1,
        "strategy": "Optimal weighted allocation",
        "description": _build_description(primary_result, feeders),
        "allocations": primary_result["allocations"],
        "total_served_mw": primary_result["total_served_mw"],
        "feeders_served": sum(1 for a in primary_result["allocations"] if a["status"] != "SHED"),
        "solve_time_ms": primary_result["solve_time_ms"],
    })

    # --- Path 2: Maximize feeder count (breadth over depth) ---
    start = time.time()
    prob2, x2, y2 = build_model(feeders, total_supply_mw, topology)
    prob2.setObjective(pulp.lpSum(y2[f["feeder_id"]] for f in feeders))
    prob2.solve(pulp.PULP_CBC_CMD(msg=0))
    solve_ms2 = (time.time() - start) * 1000

    result2 = _extract_result(prob2, x2, y2, feeders, total_supply_mw,
                              sum(f["demand_mw"] for f in feeders), solve_ms2)
    paths.append({
        "path_id": 2,
        "strategy": "Maximize feeders served (breadth)",
        "description": _build_description(result2, feeders),
        "allocations": result2["allocations"],
        "total_served_mw": result2["total_served_mw"],
        "feeders_served": sum(1 for a in result2["allocations"] if a["status"] != "SHED"),
        "solve_time_ms": result2["solve_time_ms"],
    })

    # --- Path 3: Critical loads only ---
    start = time.time()
    prob3, x3, y3 = build_model(feeders, total_supply_mw, topology)
    CRITICAL_TYPES = ("Hospital", "Water Plant", "Fire Station")
    for f in feeders:
        fid = f["feeder_id"]
        if f.get("load_type") not in CRITICAL_TYPES:
            prob3 += y3[fid] == 0, f"ForceNonCriticalOff_{fid}"
    prob3.solve(pulp.PULP_CBC_CMD(msg=0))
    solve_ms3 = (time.time() - start) * 1000

    result3 = _extract_result(prob3, x3, y3, feeders, total_supply_mw,
                              sum(f["demand_mw"] for f in feeders), solve_ms3)
    paths.append({
        "path_id": 3,
        "strategy": "Critical infrastructure only",
        "description": _build_description(result3, feeders),
        "allocations": result3["allocations"],
        "total_served_mw": result3["total_served_mw"],
        "feeders_served": sum(1 for a in result3["allocations"] if a["status"] != "SHED"),
        "solve_time_ms": result3["solve_time_ms"],
    })

    return paths


def _extract_result(prob, x, y, feeders, total_supply_mw, total_demand, solve_ms) -> dict:
    """Extract allocation result from a solved PuLP problem."""
    allocations = []
    total_served = 0.0

    for f in feeders:
        fid = f["feeder_id"]
        fraction = pulp.value(x[fid]) or 0.0
        energized = int(pulp.value(y[fid]) or 0)
        served_mw = f["demand_mw"] * fraction
        total_served += served_mw

        status = "FULL" if fraction >= 0.99 else "CURTAILED" if fraction > 0 else "SHED"

        allocations.append({
            "feeder_id": fid,
            "load_type": f.get("load_type", "Unknown"),
            "demand_mw": round(f["demand_mw"], 4),
            "served_mw": round(served_mw, 4),
            "fraction": round(fraction, 4),
            "energized": energized,
            "status": status,
            "priority_weight": f["priority_weight"],
        })

    return {
        "status": pulp.LpStatus[prob.status],
        "solve_time_ms": round(solve_ms, 2),
        "total_demand_mw": round(total_demand, 4),
        "total_supply_mw": round(total_supply_mw, 4),
        "total_served_mw": round(total_served, 4),
        "deficit_mw": round(max(0, total_demand - total_supply_mw), 4),
        "allocations": allocations,
    }


def _build_description(result: dict, feeders: list[dict]) -> str:
    """
    Build a readable description from the allocation result.
    e.g. "Hospital (205) fully served; Water Plant (206) at 73%; Industrial (207) shed.
          Total: 3.7/12.7 MW served."
    """
    parts = []
    for a in result["allocations"]:
        if a["status"] == "FULL":
            parts.append(f"{a['load_type']} ({a['feeder_id']}) fully served")
        elif a["status"] == "CURTAILED":
            parts.append(f"{a['load_type']} ({a['feeder_id']}) at {a['fraction']:.0%}")
        elif a["status"] == "SHED":
            parts.append(f"{a['load_type']} ({a['feeder_id']}) shed")

    summary = "; ".join(parts)
    summary += f". Total: {result['total_served_mw']:.1f}/{result['total_demand_mw']:.1f} MW served."
    return summary
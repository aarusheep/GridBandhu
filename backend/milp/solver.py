"""
GridBandhu — Solver
Runs the MILP and produces TOP 3 ranked allocation alternatives.
Each alternative uses a different strategy to force diverse solutions.
"""

import time
import re
import pulp
from .formulation import build_model
from backend.state.schema import store_paths, get_active_faults, get_fault_details


def solve(feeders: list[dict], total_supply_mw: float, topology: dict = None) -> dict:
    """
    Run the MILP solver and return the BEST allocation.
    Also generates 3 alternative paths and stores them in Redis.
    """
    total_demand = sum(f["demand_mw"] for f in feeders)

    faults = get_active_faults()
    if faults:
        groups = _group_overlapping_faults(faults, topology or {})
        first_primary = None
        for group in groups:
            contexts = [get_fault_details(fault_id) for fault_id in group]
            blocked = {feeder for context in contexts for feeder in _faulted_feeders(context, topology or {})}
            primary = _solve_primary(feeders, total_supply_mw, topology, blocked, total_demand)
            first_primary = first_primary or primary
            paths = _generate_alternatives(feeders, total_supply_mw, topology, primary, blocked)
            for fault_id, context in zip(group, contexts):
                fault_paths = []
                for path in paths:
                    path = dict(path)
                    path["fault_id"] = fault_id
                    path["fault_detail"] = context.get("fault_detail", "")
                    path["affected_edge_ids"] = _edge_ids_for_path(path, feeders)
                    path["description"] = _fault_description(context, path)
                    fault_paths.append(path)
                store_paths(fault_id, fault_paths)
        return first_primary

    return _solve_primary(feeders, total_supply_mw, topology, set(), total_demand)


def _solve_primary(feeders, supply, topology, blocked, total_demand):
    start = time.time()
    prob, x, y = build_model(feeders, supply, topology, blocked)
    prob.solve(pulp.PULP_CBC_CMD(msg=0))
    return _extract_result(prob, x, y, feeders, supply, total_demand, (time.time() - start) * 1000)


def _faulted_feeders(context, topology):
    ids = set()
    for value in (context.get("edge_id", ""), context.get("node_id", "")):
        match = re.search(r"/([0-9]{3})/P", str(value))
        if match:
            ids.add(match.group(1))
    for edge in topology.get("edges", []):
        if context.get("node_id") in {edge.get("from"), edge.get("to")}:
            ids.update(_feeder_ids_from_edge(edge.get("id", "")))
            ids.update(_feeder_ids_from_connected_graph(edge, topology))
    return ids


def _feeder_ids_from_edge(edge_id):
    match = re.search(r"/([0-9]{3})/P", str(edge_id))
    return {match.group(1)} if match else set()


def _feeder_ids_from_connected_graph(start_edge, topology):
    """Walk from a node/facility through DTC links until an HT feeder is found."""
    frontier = {start_edge.get("from"), start_edge.get("to")}
    visited = set(frontier)
    found = set()
    for _ in range(len(topology.get("edges", [])) + 1):
        next_frontier = set()
        for edge in topology.get("edges", []):
            endpoints = {edge.get("from"), edge.get("to")}
            if endpoints.intersection(frontier):
                found.update(_feeder_ids_from_edge(edge.get("id", "")))
                next_frontier.update(endpoints - visited)
        if not next_frontier:
            break
        visited.update(next_frontier)
        frontier = next_frontier
    return found


def _fault_assets(fault_id, topology):
    context = get_fault_details(fault_id)
    assets = {context.get("edge_id"), context.get("node_id")}
    for edge in topology.get("edges", []):
        if context.get("node_id") in {edge.get("from"), edge.get("to")}:
            assets.add(edge.get("id"))
    return {str(asset) for asset in assets if asset}


def _group_overlapping_faults(faults, topology):
    groups = []
    for fault_id in faults:
        assets = _fault_assets(fault_id, topology)
        feeders = _faulted_feeders(get_fault_details(fault_id), topology)
        for group in groups:
            if assets.intersection(group["assets"]) or feeders.intersection(group["feeders"]):
                group["faults"].append(fault_id)
                group["assets"].update(assets)
                group["feeders"].update(feeders)
                break
        else:
            groups.append({"faults": [fault_id], "assets": set(assets), "feeders": set(feeders)})
    return [group["faults"] for group in groups]


def _fault_description(context, path):
    target = context.get("node_id") or context.get("edge_id") or "network asset"
    kind = context.get("fault_type", "fault").replace("_", " ")
    return f"Response to {kind} at {target}: {path['description']}"


def _edge_ids_for_path(path: dict, feeders: list[dict]) -> list[str]:
    """Map the MILP allocation strategy back to real topology edge IDs."""
    served = {item["feeder_id"] for item in path["allocations"] if item["status"] != "SHED"}
    edge_ids = []
    for feeder in feeders:
        if feeder["feeder_id"] in served:
            edge_ids.extend(str(edge_id) for edge_id in feeder.get("edge_ids", []))
    return list(dict.fromkeys(edge_ids))


def _generate_alternatives(feeders, total_supply_mw, topology, primary_result, blocked_feeders=None) -> list[dict]:
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
    prob2, x2, y2 = build_model(feeders, total_supply_mw, topology, blocked_feeders)
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
    prob3, x3, y3 = build_model(feeders, total_supply_mw, topology, blocked_feeders)
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

"""
GridShield -- MILP Formulation
Runs only when faults:active is non-empty (fault-driven reallocation).
Hospitals & Water Plants are never fully shed -- only curtailed down to
their min safe fraction -- unless their own relay has tripped.
"""

import pulp
from .weights import DEPENDENCIES

NEVER_SHED_TYPES = ("Hospital", "Water Plant")


def build_model(feeders: list[dict], total_supply_mw: float, topology: dict = None, unavailable_feeders: set[str] | None = None) -> tuple:
    prob = pulp.LpProblem("GridShield_LoadAllocation", pulp.LpMaximize)

    # x[fid] = fraction of demand served (continuous, 0-1)
    # y[fid] = whether the feeder is energized at all (binary)
    x = {}
    y = {}
    unavailable_feeders = unavailable_feeders or set()
    for f in feeders:
        fid = f["feeder_id"]
        x[fid] = pulp.LpVariable(f"serve_{fid}", lowBound=0, upBound=1, cat="Continuous")
        y[fid] = pulp.LpVariable(f"on_{fid}", cat="Binary")

    # Objective: maximize priority-weighted, severity-scaled load served
    prob += pulp.lpSum(
        f["priority_weight"] * f["severity_multiplier"] * f["demand_mw"] * x[f["feeder_id"]]
        for f in feeders
    ), "MaxWeightedLoadServed"

    # Total power drawn (including losses) cannot exceed available supply
    prob += (
        pulp.lpSum(f["demand_mw"] * x[f["feeder_id"]] * f["loss_factor"] for f in feeders)
        <= total_supply_mw,
        "TotalSupplyLimit",
    )

    for f in feeders:
        fid = f["feeder_id"]

        # A feeder cannot be served beyond its physical capacity
        prob += f["demand_mw"] * x[fid] <= f["capacity_mw"], f"FeederCapacity_{fid}"

        # A feeder can only be served if it is energized
        prob += x[fid] <= y[fid], f"LinkXY_upper_{fid}"

        # If energized, a feeder must receive at least its minimum safe fraction
        if f["min_safe_fraction"] > 0:
            prob += x[fid] >= f["min_safe_fraction"] * y[fid], f"MinSafe_{fid}"

    # Dependent feeders can only be energized if their upstream provider is energized
    for dependent, provider in DEPENDENCIES.items():
        if dependent in y and provider in y:
            prob += y[dependent] <= y[provider], f"Dependency_{dependent}_needs_{provider}"

    # A feeder with a tripped or faulted relay must stay de-energized
    for f in feeders:
        fid = f["feeder_id"]
        relay = f.get("relay_status", "NORMAL")
        if relay in ("TRIPPED", "FAULT"):
            prob += y[fid] == 0, f"RelayLockout_{fid}"

    # Critical loads (hospitals, water plants) are never fully shed,
    # unless their own relay has tripped -- in which case the lockout above wins
    for f in feeders:
        fid = f["feeder_id"]
        relay = f.get("relay_status", "NORMAL")
        if f.get("load_type") in NEVER_SHED_TYPES and relay not in ("TRIPPED", "FAULT") and fid not in unavailable_feeders:
            prob += y[fid] == 1, f"NeverShed_{fid}"

        if fid in unavailable_feeders:
            prob += y[fid] == 0, f"FaultedFeederLockout_{fid}"

    if topology is not None:
        _add_topology_constraints(prob, x, y, feeders, topology)

    return prob, x, y


def _add_topology_constraints(prob, x, y, feeders, topology):
    """
    Adds reconfiguration constraints when a network topology is available:
    per-edge capacity limits and a radiality (tree) constraint on switches.
    """
    edges = topology.get("edges", [])
    switches = topology.get("switches", [])
    if not edges:
        return

    feeder_ids = {f["feeder_id"] for f in feeders}

    # One binary variable per switch: closed (1) or open (0)
    s = {}
    for sw in switches:
        sid = sw["switch_id"]
        s[sid] = pulp.LpVariable(f"switch_{sid}", cat="Binary")
        if not sw.get("is_switchable", True):
            prob += s[sid] == 1, f"FixedSwitch_{sid}"

    # Edge capacity limits for edges that feed directly into a feeder
    for edge in edges:
        from_n, to_n = edge["from_node"], edge["to_node"]
        cap_mw = edge.get("capacity_kw", 10000) / 1000.0
        if to_n in feeder_ids:
            f_match = next((f for f in feeders if f["feeder_id"] == to_n), None)
            if f_match:
                prob += f_match["demand_mw"] * x[to_n] <= cap_mw, f"EdgeCapacity_{from_n}_{to_n}"

    # Radiality: the number of closed switches should keep the network a tree
    # (nodes - 1 edges), avoiding loops in the reconfigured network
    if len(s) > 1:
        node_count = len(topology.get("nodes", []))
        if node_count > 1:
            prob += pulp.lpSum(s[sid] for sid in s) == node_count - 1, "Radiality_TreeConstraint"

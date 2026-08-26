"""
GridShield — Allocation Diff & Reason Code Generator
Compares previous allocation with new one and produces
plain-language explanations for each change.
"""


def generate_diff(old_allocations: list[dict], new_allocations: list[dict]) -> list[dict]:
    """
    Compare two allocation results and generate reason codes.

    Args:
        old_allocations: previous solver output (list of feeder dicts)
        new_allocations: current solver output (list of feeder dicts)

    Returns:
        list of dicts with feeder_id, action, reason
    """

    old_map = {a["feeder_id"]: a for a in old_allocations}
    changes = []

    for new in new_allocations:
        fid = new["feeder_id"]
        old = old_map.get(fid)

        if old is None:
            changes.append({
                "feeder_id": fid,
                "action": "NEW",
                "old_status": None,
                "new_status": new["status"],
                "served_mw": new["served_mw"],
                "reason": f"{new['load_type']} feeder added to allocation.",
            })
            continue

        if old["status"] == new["status"] and abs(old["fraction"] - new["fraction"]) < 0.01:
            continue

        if old["status"] == "SHED" and new["status"] in ("FULL", "CURTAILED"):
            changes.append({
                "feeder_id": fid,
                "action": "RESTORED",
                "old_status": old["status"],
                "new_status": new["status"],
                "served_mw": new["served_mw"],
                "reason": f"Power restored to {new['load_type']} feeder {fid} — "
                          f"now receiving {new['fraction']:.0%} of demand ({new['served_mw']:.2f} MW).",
            })

        elif new["status"] == "SHED" and old["status"] != "SHED":
            changes.append({
                "feeder_id": fid,
                "action": "SHED",
                "old_status": old["status"],
                "new_status": new["status"],
                "served_mw": 0,
                "reason": f"{new['load_type']} feeder {fid} shed — "
                          f"priority {new['priority_weight']:.1f} insufficient under current supply constraint. "
                          f"Previously serving {old['served_mw']:.2f} MW.",
            })

        elif new["fraction"] < old["fraction"] - 0.01:
            drop_mw = old["served_mw"] - new["served_mw"]
            changes.append({
                "feeder_id": fid,
                "action": "CURTAILED",
                "old_status": old["status"],
                "new_status": new["status"],
                "served_mw": new["served_mw"],
                "reason": f"{new['load_type']} feeder {fid} curtailed by {drop_mw:.2f} MW — "
                          f"now at {new['fraction']:.0%} of demand. "
                          f"Higher-priority loads consuming available capacity.",
            })

        elif new["fraction"] > old["fraction"] + 0.01:
            gain_mw = new["served_mw"] - old["served_mw"]
            changes.append({
                "feeder_id": fid,
                "action": "INCREASED",
                "old_status": old["status"],
                "new_status": new["status"],
                "served_mw": new["served_mw"],
                "reason": f"{new['load_type']} feeder {fid} allocation increased by {gain_mw:.2f} MW — "
                          f"now at {new['fraction']:.0%} of demand.",
            })

    return changes


def format_summary(result: dict, changes: list[dict]) -> str:
    """
    Generate a one-line operator-facing summary.
    """
    shed_count = sum(1 for a in result["allocations"] if a["status"] == "SHED")
    curtailed_count = sum(1 for a in result["allocations"] if a["status"] == "CURTAILED")
    full_count = sum(1 for a in result["allocations"] if a["status"] == "FULL")

    parts = []
    if full_count:
        parts.append(f"{full_count} fully served")
    if curtailed_count:
        parts.append(f"{curtailed_count} curtailed")
    if shed_count:
        parts.append(f"{shed_count} shed")

    summary = f"Allocation: {', '.join(parts)}. "
    summary += f"Serving {result['total_served_mw']:.1f}/{result['total_demand_mw']:.1f} MW "
    summary += f"({result['total_served_mw']/result['total_demand_mw']:.0%})."

    return summary
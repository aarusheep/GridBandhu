"""End-to-end telemetry test producer.

Runs normal telemetry from the PostgreSQL topology, then deliberately trips
one real PostgreSQL-backed HT edge so the detector and UI can be exercised.
It does not create topology records or bypass PostgreSQL IDs.
"""

from __future__ import annotations

import argparse
import logging
import time
from copy import deepcopy

from backend.data_ingestion_layer.live_data import get_redis_client, load_baselines, run_cycle


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gridbandhu-fault-test")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run normal telemetry, then a detector-visible fault")
    parser.add_argument("--normal-seconds", type=int, default=60)
    parser.add_argument("--fault-seconds", type=int, default=120)
    parser.add_argument("--interval-seconds", type=int, default=5)
    parser.add_argument("--edge-id", default=None, help="Postgres HT edge ID; defaults to the first HT edge")
    parser.add_argument(
        "--fault-mode",
        choices=("protection_trip", "overload", "full_outage"),
        default="protection_trip",
        help="Detector condition to inject (default: protection_trip)",
    )
    parser.add_argument(
        "--node-id",
        default=None,
        help="Facility or DTC node ID from PostgreSQL; injects node telemetry instead of an edge fault",
    )
    parser.add_argument(
        "--node-fault-mode",
        choices=("high_load", "undervoltage", "high_thd"),
        default="high_load",
        help="Node telemetry condition when --node-id is supplied",
    )
    args = parser.parse_args()

    baselines = load_baselines()
    redis_client = get_redis_client()
    edge_ids = list(baselines["edges"])
    # Give each fault mode a different real PostgreSQL-backed location by
    # default. An explicit --edge-id always takes precedence.
    default_edge_position = {
        "protection_trip": 0,
        "overload": 1,
        "full_outage": 2,
    }[args.fault_mode]
    edge_id = args.edge_id or (edge_ids[default_edge_position] if len(edge_ids) > default_edge_position else None)
    if args.node_id:
        valid_nodes = {**baselines["dtc"], **baselines["facilities"]}
        if args.node_id not in valid_nodes:
            raise RuntimeError(f"Node {args.node_id!r} is not present in the PostgreSQL topology")
    else:
        if not edge_id:
            raise RuntimeError("Postgres returned no HT edges")
        if edge_id not in baselines["edges"]:
            raise RuntimeError(f"Edge {edge_id!r} is not present in the Postgres topology")

    normal = deepcopy(baselines)
    for edge in normal["edges"].values():
        edge["needs_review"] = False

    log.info("Normal telemetry phase for %ss; edge under test: %s", args.normal_seconds, edge_id)
    normal_until = time.monotonic() + args.normal_seconds
    while time.monotonic() < normal_until:
        run_cycle(normal)
        time.sleep(args.interval_seconds)

    if args.node_id:
        node_base = {**baselines["dtc"], **baselines["facilities"]}[args.node_id]
        node_capacity = node_base.get("capacity_kw", node_base.get("typical_load_kw", 0))
        node_fault_reading = {
            "high_load": {
                "current_load_kw": round(node_capacity * 1.25, 3),
                "load_kw": round(node_capacity * 1.25, 3),
                "loading_percentage": 125.0,
                "voltage_pu": 0.97,
                "thd_percentage": 2.0,
                "status": "fault",
            },
            "undervoltage": {
                "current_load_kw": round(node_capacity * 0.70, 3),
                "load_kw": round(node_capacity * 0.70, 3),
                "loading_percentage": 70.0,
                "voltage_pu": 0.84,
                "thd_percentage": 2.0,
                "status": "fault",
            },
            "high_thd": {
                "current_load_kw": round(node_capacity * 0.70, 3),
                "load_kw": round(node_capacity * 0.70, 3),
                "loading_percentage": 70.0,
                "voltage_pu": 0.98,
                "thd_percentage": 8.0,
                "status": "fault",
            },
        }[args.node_fault_mode]
    else:
        node_fault_reading = None
        edge_capacity = baselines["edges"][edge_id]["capacity_kw"]
    fault_reading = {
        "protection_trip": {
            "current_flow_kw": 0,
            "switch_state": "open",
            "protective_relay_status": "PICKUP",
        },
        "overload": {
            "current_flow_kw": round(edge_capacity * 1.25, 3),
            "switch_state": "closed",
            "protective_relay_status": "NORMAL",
        },
        "full_outage": {
            "current_flow_kw": 0,
            "switch_state": "closed",
            "protective_relay_status": "NORMAL",
        },
    }[args.fault_mode] if not args.node_id else None

    log.warning(
        "Fault phase started: %s on edge %s for %ss",
        args.node_fault_mode if args.node_id else args.fault_mode,
        args.node_id if args.node_id else edge_id,
        args.fault_seconds,
    )
    fault_until = time.monotonic() + args.fault_seconds
    while time.monotonic() < fault_until:
        if args.node_id:
            redis_client.hset(f"node:{args.node_id}", mapping={**node_fault_reading, "test_injected": "true"})
        else:
            redis_client.hset(f"edge:{edge_id}", mapping={**fault_reading, "test_injected": "true"})
        # Keep all non-faulted assets moving while the selected edge remains faulty.
        run_cycle(normal)
        if args.node_id:
            redis_client.hset(f"node:{args.node_id}", mapping={**node_fault_reading, "test_injected": "true"})
        else:
            redis_client.hset(f"edge:{edge_id}", mapping={**fault_reading, "test_injected": "true"})
        time.sleep(args.interval_seconds)

    log.info("Fault phase complete; writing normal telemetry so the detector can clear it")
    run_cycle(normal)


if __name__ == "__main__":
    main()

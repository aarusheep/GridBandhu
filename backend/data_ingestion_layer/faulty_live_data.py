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
    args = parser.parse_args()

    baselines = load_baselines()
    redis_client = get_redis_client()
    edge_id = args.edge_id or next(iter(baselines["edges"]), None)
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

    log.warning("Fault phase started: relay trip on edge %s for %ss", edge_id, args.fault_seconds)
    fault_until = time.monotonic() + args.fault_seconds
    while time.monotonic() < fault_until:
        redis_client.hset(f"edge:{edge_id}", mapping={
            "current_flow_kw": 0,
            "switch_state": "open",
            "protective_relay_status": "PICKUP",
            "test_injected": "true",
        })
        # Keep all non-faulted assets moving while the selected edge remains faulty.
        run_cycle(normal)
        redis_client.hset(f"edge:{edge_id}", mapping={
            "current_flow_kw": 0,
            "switch_state": "open",
            "protective_relay_status": "PICKUP",
            "test_injected": "true",
        })
        time.sleep(args.interval_seconds)

    log.info("Fault phase complete; writing normal telemetry so the detector can clear it")
    run_cycle(normal)


if __name__ == "__main__":
    main()

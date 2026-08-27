"""GridBandhu API: topology, telemetry, anomaly and decision simulation."""

from __future__ import annotations

import asyncio
import csv
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import logging
import math
import os
import random
import re
import time
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from backend.data_ingestion_layer.ingestion_api import router as ingestion_router
from backend.milp.grid import router as milp_router
from backend.security.auth import authenticate, authenticate_websocket, create_token, credentials_match
from backend.security.rbac import require_permission

try:
    import psycopg2
except ImportError:  # The CSV demo fallback remains usable without DB packages.
    psycopg2 = None
try:
    import redis
except ImportError:
    redis = None


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "dataset2.0"
load_dotenv(ROOT / ".env")
logger = logging.getLogger("gridbandhu")


class LoginRequest(BaseModel):
    username: str
    password: str


class DecisionAction(BaseModel):
    action: str = "accept"


class TestFaultRequest(BaseModel):
    edge_id: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_number(value: str, low: float, high: float) -> float:
    digest = int(hashlib.sha1(value.encode()).hexdigest()[:8], 16)
    return round(low + (digest % 1000) / 1000 * (high - low), 1)


def load_demo_topology() -> dict[str, Any]:
    """Build a normalized topology from the supplied dataset for local demos.

    PostGIS remains the production source of truth; this fallback makes the
    full API/UI contract testable before infrastructure is started.
    """
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, str]] = []
    with DATASET.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    for row in rows:
        sub_id = f"SUB_{row['SUBSTATION_CODE']}"
        dtc_id = f"DTC_{row['DTC_CODE']}"
        pole_from = f"POLE_{row['INITIAL_POLE']}"
        pole_to = f"POLE_{row['DESTINATION_POLE']}"
        facility_name = row.get("CONNECTED_FACILITY_NAME", "").strip()
        facility_id = "FAC_" + "_".join("".join(c if c.isalnum() else "_" for c in facility_name.upper()).split("_"))[:42] if facility_name else ""

        nodes.setdefault(sub_id, {"id": sub_id, "type": "substation", "name": row["SUBSTATION_NAME"], "lat": float(row["INITIAL_POLE_LATITUDE"]), "lng": float(row["INITIAL_POLE_LONGITUDE"]), "status": "healthy", "load": 61.0})
        nodes.setdefault(pole_from, {"id": pole_from, "type": "pole", "name": row["INITIAL_POLE"], "lat": float(row["INITIAL_POLE_LATITUDE"]), "lng": float(row["INITIAL_POLE_LONGITUDE"]), "status": "healthy", "load": 0})
        nodes.setdefault(pole_to, {"id": pole_to, "type": "pole", "name": row["DESTINATION_POLE"], "lat": float(row["DESTINATION_POLE_LATITUDE"]), "lng": float(row["DESTINATION_POLE_LONGITUDE"]), "status": "healthy", "load": 0})
        nodes.setdefault(dtc_id, {"id": dtc_id, "type": "dtc", "name": row["DTC_NAME"], "lat": float(row["DESTINATION_POLE_LATITUDE"]), "lng": float(row["DESTINATION_POLE_LONGITUDE"]), "status": "healthy", "load": stable_number(dtc_id, 48, 79), "capacity": float(row["DTC_CAPACITY"])})
        if facility_id:
            nodes.setdefault(facility_id, {"id": facility_id, "type": "facility", "name": facility_name, "lat": float(row["FACILITY_LATITUDE"]), "lng": float(row["FACILITY_LONGITUDE"]), "status": "healthy", "load": stable_number(facility_id, 34, 72), "facility_type": row.get("CONNECTED_FACILITY_TYPE", "consumer")})

        edge_id = f"EDGE_{row['HT_POLE_ID']}"
        edges.setdefault(edge_id, {"id": edge_id, "from": pole_from, "to": pole_to, "type": "ht", "voltage": float(row["VOLTAGE_KV"]), "capacity": round(float(row["TRANSFORMER_PTR_CAPACITY_MVA"]) * float(row["POWER_FACTOR"]) * 1000, 1), "status": "healthy", "flow": stable_number(edge_id, 42, 74), "coordinates": [[float(row["INITIAL_POLE_LONGITUDE"]), float(row["INITIAL_POLE_LATITUDE"])], [float(row["DESTINATION_POLE_LONGITUDE"]), float(row["DESTINATION_POLE_LATITUDE"])]]})
        edges.setdefault(f"EDGE_{row['DTC_CODE']}", {"id": f"EDGE_{row['DTC_CODE']}", "from": pole_to, "to": dtc_id, "type": "dt", "voltage": float(row["VOLTAGE_KV"]), "capacity": float(row["DTC_CAPACITY"]), "status": "healthy", "flow": stable_number(dtc_id, 35, 65), "coordinates": [[float(row["DESTINATION_POLE_LONGITUDE"]), float(row["DESTINATION_POLE_LATITUDE"])], [float(row["DESTINATION_POLE_LONGITUDE"]) + 0.00035, float(row["DESTINATION_POLE_LATITUDE"]) + 0.00025]]})
        if facility_id:
            edge_id = f"EDGE_LT_{row['DTC_CODE']}_{facility_id}"
            edges.setdefault(edge_id, {"id": edge_id, "from": dtc_id, "to": facility_id, "type": "lt", "voltage": 0.415, "capacity": 200, "status": "healthy", "flow": stable_number(edge_id, 24, 58), "coordinates": [[float(row["DESTINATION_POLE_LONGITUDE"]) + 0.00035, float(row["DESTINATION_POLE_LATITUDE"]) + 0.00025], [float(row["FACILITY_LONGITUDE"]), float(row["FACILITY_LATITUDE"])] ]})

    # Keep the demo legible while still using every source row to discover nodes.
    return {"nodes": list(nodes.values()), "edges": list(edges.values()), "source": "dataset2.0 demo fallback"}


def load_postgres_topology() -> dict[str, Any] | None:
    """Read the normalized topology tables when explicitly enabled."""
    if psycopg2 is None:
        raise RuntimeError("PostgreSQL topology is required: install psycopg2-binary")
    conn = psycopg2.connect(host=os.getenv("POSTGRES_HOST", "localhost"), port=os.getenv("POSTGRES_PORT", 5432), dbname=os.getenv("POSTGRES_DB", "gridbandhu"), user=os.getenv("POSTGRES_USER", "postgres"), password=os.getenv("POSTGRES_PASSWORD", ""))
    cur = conn.cursor(); nodes: list[dict[str, Any]] = []; edges: list[dict[str, Any]] = []; node_by_id: dict[str, dict[str, Any]] = {}; edge_by_id: dict[str, dict[str, Any]] = {}
    cur.execute("SELECT substation_id, name, ST_Y(location::geometry), ST_X(location::geometry) FROM substations")
    nodes.extend({"id": f"SUB_{r[0]}", "type": "substation", "name": r[1], "lat": float(r[2]), "lng": float(r[3]), "status": "healthy", "load": 61} for r in cur.fetchall())
    node_by_id.update({node["id"]: node for node in nodes})
    cur.execute("SELECT edge_id, from_node_id, to_node_id, substation_id, ST_AsGeoJSON(path), ST_Y(from_location::geometry), ST_X(from_location::geometry), ST_Y(to_location::geometry), ST_X(to_location::geometry), voltage_kv, capacity_kw FROM ht_edges")
    for edge_id, from_id, to_id, substation_id, geometry, from_lat, from_lng, to_lat, to_lng, voltage, capacity in cur.fetchall():
        coordinates = json.loads(geometry)["coordinates"]
        from_key, to_key = f"POLE_{from_id}", f"POLE_{to_id}"
        node_by_id.setdefault(from_key, {"id": from_key, "type": "pole", "name": str(from_id), "lat": float(from_lat), "lng": float(from_lng), "status": "healthy", "load": 0})
        node_by_id.setdefault(to_key, {"id": to_key, "type": "pole", "name": str(to_id), "lat": float(to_lat), "lng": float(to_lng), "status": "healthy", "load": 0})
        edge = {"id": edge_id, "from": from_key, "to": to_key, "type": "ht", "voltage": float(voltage), "capacity": float(capacity), "status": "healthy", "flow": 55, "coordinates": coordinates}
        edges.append(edge); edge_by_id[str(edge_id)] = edge
    nodes.extend(node for node_id, node in node_by_id.items() if node_id.startswith("POLE_"))
    cur.execute("SELECT d.dtc_id, d.name, d.parent_ht_edge_id, ST_Y(e.to_location::geometry), ST_X(e.to_location::geometry) FROM dtc d LEFT JOIN ht_edges e ON e.edge_id = d.parent_ht_edge_id")
    for dtc_id, name, parent_edge_id, lat, lng in cur.fetchall():
        dtc_key = f"DTC_{dtc_id}"; dtc_node = {"id": dtc_key, "type": "dtc", "name": name, "lat": float(lat), "lng": float(lng), "status": "healthy", "load": 60}; nodes.append(dtc_node); node_by_id[dtc_key] = dtc_node
        parent = edge_by_id.get(str(parent_edge_id))
        if parent and parent["coordinates"]:
            parent_point = parent["coordinates"][-1]
            edges.append({"id": f"EDGE_DTC_{dtc_id}", "from": parent["to"], "to": dtc_key, "type": "dt", "voltage": parent["voltage"], "capacity": parent["capacity"], "status": "healthy", "flow": 55, "coordinates": [parent_point, [float(lng), float(lat)]]})
    cur.execute("SELECT facility_id, name, facility_type, connected_dtc_id, ST_Y(location::geometry), ST_X(location::geometry) FROM facilities")
    for facility_id, name, facility_type, dtc_id, lat, lng in cur.fetchall():
        fac_node = {"id": facility_id, "type": "facility", "name": name, "facility_type": facility_type, "lat": float(lat), "lng": float(lng), "status": "healthy", "load": 45}; nodes.append(fac_node)
        parent = node_by_id.get(f"DTC_{dtc_id}")
        if parent:
            edges.append({"id": f"EDGE_LT_{facility_id}", "from": parent["id"], "to": facility_id, "type": "lt", "voltage": 0.415, "capacity": 200, "status": "healthy", "flow": 40, "coordinates": [[parent["lng"], parent["lat"]], [float(lng), float(lat)]]})
    cur.close(); conn.close()
    return {"nodes": nodes, "edges": edges, "source": "PostgreSQL/PostGIS"}


try:
    topology = load_postgres_topology()
except Exception as exc:
    raise RuntimeError(f"GridBandhu requires a reachable Postgres/PostGIS topology database: {exc}") from exc
if not topology or not topology["nodes"]:
    raise RuntimeError("GridBandhu requires topology rows in Postgres; no database fallback is enabled")
roads_loaded = False


def route_edges_through_roads() -> None:
    """Replace direct segments with road-following geometry from OSRM.

    If routing is unavailable, the edge retains its original database segment
    and is marked so the UI can distinguish it from a road-routed path.
    """
    global roads_loaded
    if roads_loaded or os.getenv("GRIDBANDHU_ROUTE_ROADS", "true").lower() != "true":
        return
    def distance_km(first: list[float], second: list[float]) -> float:
        dx = (second[0] - first[0]) * 111 * math.cos(math.radians((first[1] + second[1]) / 2))
        dy = (second[1] - first[1]) * 111
        return math.sqrt(dx * dx + dy * dy)
    routable = [edge for edge in topology["edges"] if edge.get("type") in {"ht", "dt", "lt"} and len(edge.get("coordinates", [])) >= 2]

    def route_one(edge: dict[str, Any]) -> tuple[dict[str, Any], list[list[float]] | None, str | None, float | None, float | None]:
        start, end = edge["coordinates"][0], edge["coordinates"][-1]
        query = urllib.parse.urlencode({"overview": "full", "geometries": "geojson"})
        try:
            def fetch_route(first: list[float], second: list[float]) -> dict[str, Any]:
                url = f"https://router.project-osrm.org/route/v1/driving/{first[0]},{first[1]};{second[0]},{second[1]}?{query}"
                request = urllib.request.Request(url, headers={"User-Agent": "GridBandhu/0.1"})
                with urllib.request.urlopen(request, timeout=2.5) as response:
                    return json.loads(response.read().decode("utf-8"))

            result = fetch_route(start, end)
            if result.get("routes"):
                routed = result["routes"][0]["geometry"]["coordinates"]
                route_length = sum(distance_km(routed[index - 1], routed[index]) for index in range(1, len(routed)))
                direct_length = distance_km(start, end)
                # Short utility connections can legitimately follow a road
                # network several times farther than their straight-line
                # distance. Allow that urban detour, but reject OSRM matches
                # that jump to a distant/unrelated road.
                if route_length <= max(direct_length * 10, direct_length + 2.0):
                    return edge, [start, *routed[1:-1], end], None, route_length, direct_length
                # Some utility endpoints sit slightly away from a routable
                # road. Snap both ends to their nearest road and retry before
                # giving up; this avoids drawing a false straight connection.
                nearest = []
                for point in (start, end):
                    nearest_url = f"https://router.project-osrm.org/nearest/v1/driving/{point[0]},{point[1]}?number=1"
                    nearest_request = urllib.request.Request(nearest_url, headers={"User-Agent": "GridBandhu/0.1"})
                    with urllib.request.urlopen(nearest_request, timeout=2.5) as response:
                        nearest_result = json.loads(response.read().decode("utf-8"))
                    nearest.append(nearest_result.get("waypoints", [{}])[0].get("location"))
                if nearest[0] and nearest[1]:
                    snapped = fetch_route(nearest[0], nearest[1])
                    if snapped.get("routes"):
                        routed = snapped["routes"][0]["geometry"]["coordinates"]
                        route_length = sum(distance_km(routed[index - 1], routed[index]) for index in range(1, len(routed)))
                        if route_length <= max(direct_length * 30, direct_length + 4.0):
                            return edge, [start, *routed, end], None, route_length, direct_length
                    return edge, None, f"Rejected excessive detour ({route_length:.1f} km for {direct_length:.1f} km direct)", None, None
                return edge, None, f"Rejected excessive detour ({route_length:.1f} km for {direct_length:.1f} km direct)", None, None
            return edge, None, "OSRM returned no route", None, None
        except Exception:
            return edge, None, "OSRM route unavailable; inspect backend logs", None, None

    # Parallelize the independent edge requests, but keep a small worker pool
    # so a basemap reload does not overwhelm the public routing service.
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(route_one, routable))
    for edge, coordinates, error, route_length, direct_length in results:
        if coordinates:
            edge["coordinates"] = coordinates
            edge["road_route_km"] = round(route_length, 3)
            edge["road_direct_km"] = round(direct_length, 3)
            edge["road_routed"] = True
        else:
            edge["road_routed"] = False
            edge["routing_error"] = error or "OSRM route unavailable; inspect backend logs"
            logger.warning("Road routing failed for %s: %s", edge.get("id"), edge["routing_error"])
    roads_loaded = True
events: list[dict[str, Any]] = []
connections: set[WebSocket] = set()
active_anomaly: dict[str, Any] | None = None
active_anomalies: dict[str, dict[str, Any]] = {}
active_solution: dict[str, Any] | None = None


def add_event(kind: str, message: str, severity: str = "info", **extra: Any) -> dict[str, Any]:
    event = {"id": f"EVT-{int(time.time() * 1000)}", "type": kind, "message": message, "severity": severity, "timestamp": now_iso(), **extra}
    events.insert(0, event)
    del events[30:]
    return event


async def broadcast(payload: dict[str, Any]) -> None:
    stale: set[WebSocket] = set()
    for socket in connections:
        try:
            await socket.send_json(payload)
        except Exception:
            stale.add(socket)
    connections.difference_update(stale)


def create_redis_client():
    if redis is None:
        raise RuntimeError("Redis telemetry is required: install redis")
    return redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", 6379)), db=int(os.getenv("REDIS_DB", 0)), decode_responses=True, protocol=2)


def detector_is_online(redis_client) -> bool:
    return bool(redis_client.exists("detection:heartbeat"))


def fault_from_redis(redis_client, fault_id: str) -> dict[str, Any] | None:
    details = redis_client.hgetall(f"fault:{fault_id}")
    node_id = details.get("node_id")
    if node_id:
        connected_edges = [
            edge["id"] for edge in topology["edges"]
            if node_id in {edge["from"], edge["to"]}
        ]
        return {
            "id": fault_id,
            "type": details.get("fault_type", "node_fault"),
            "severity": "critical",
            "title": f"{details.get('fault_type', 'Node fault').replace('_', ' ').title()} detected",
            "reason": details.get("path_description", "Detection layer reported abnormal node telemetry"),
            "source_node_id": node_id,
            "affected_node_ids": [node_id],
            "affected_edge_ids": connected_edges,
            "detected_at": details.get("detected_at", now_iso()),
            "status": "open",
            "node_id": node_id,
        }
    edge_id = details.get("edge_id")
    if not edge_id:
        return None
    edge = next((item for item in topology["edges"] if str(item["id"]) == str(edge_id)), None)
    if not edge:
        return None
    affected_edges = [edge["id"]]
    for candidate in topology["edges"]:
        if candidate["id"] != edge["id"] and (candidate["from"] in {edge["from"], edge["to"]} or candidate["to"] in {edge["from"], edge["to"]}):
            affected_edges.append(candidate["id"])
    affected_nodes = {edge["from"], edge["to"]}
    if details.get("dtc_id"):
        affected_nodes.add(f"DTC_{details['dtc_id']}")
    return {"id": fault_id, "type": details.get("fault_type", "fault"), "severity": "critical", "title": f"{details.get('fault_type', 'Fault').replace('_', ' ').title()} detected", "reason": details.get("path_description", "Detection layer reported an affected network edge"), "source_node_id": f"DTC_{details['dtc_id']}" if details.get("dtc_id") else edge["from"], "affected_node_ids": list(affected_nodes), "affected_edge_ids": affected_edges, "detected_at": details.get("detected_at", now_iso()), "status": "open", "edge_id": edge_id}


def get_runtime_anomaly(anomaly_id: str, client=None) -> dict[str, Any] | None:
    """Resolve an active anomaly from memory first, then Redis as source of truth."""
    item = active_anomalies.get(anomaly_id)
    if item:
        return item
    if active_anomaly and active_anomaly.get("id") == anomaly_id:
        return active_anomaly
    client = client or create_redis_client()
    if not detector_is_online(client) or not client.sismember("faults:active", anomaly_id):
        return None
    return fault_from_redis(client, anomaly_id)


async def run_milp_for_active_fault() -> None:
    """Run the real MILP solver and persist its alternatives in Redis."""
    try:
        from backend.milp.solver import solve
        from backend.state.schema import get_all_feeders, get_total_supply

        feeders = await asyncio.to_thread(get_all_feeders)
        supply = await asyncio.to_thread(get_total_supply)
        if not feeders:
            raise RuntimeError("MILP received no feeder telemetry from Redis/Postgres")
        milp_topology = {
            "nodes": [{"id": node["id"]} for node in topology["nodes"]],
            "edges": [{"from_node": edge["from"], "to_node": edge["to"], "capacity_kw": edge.get("capacity", 0)} for edge in topology["edges"]],
            "switches": [],
        }
        result = await asyncio.to_thread(solve, feeders, supply, milp_topology)
        logger.info("MILP solved: %s, served %.3f/%.3f MW", result.get("status"), result.get("total_served_mw", 0), result.get("total_demand_mw", 0))
    except Exception as exc:
        logger.exception("MILP solve failed")
        await broadcast({"type": "integration_error", "message": f"MILP solve failed: {exc}", "severity": "critical", "timestamp": now_iso()})


def apply_control_command(command: dict[str, Any], client) -> None:
    """Simulation control layer: execute a resolution command in Redis."""
    anomalies = command.get("anomalies") or [command.get("anomaly", {})]
    for anomaly in anomalies:
        _restore_controlled_asset(anomaly, client)
    logger.info("Control command executed: %s for %d fault(s)", command.get("command_id"), len(anomalies))


def _restore_controlled_asset(anomaly: dict[str, Any], client) -> None:
    """Restore one asset and atomically retire its resolved fault record.

    The control layer also stops the test producer from writing the old fault
    back into Redis after the accepted solution has been executed.
    """
    fault_id = anomaly.get("id")
    if fault_id:
        # The faulty-live-data test producer checks this marker before every
        # injection cycle. It is intentionally scoped to the fault so other
        # active tests are not interrupted.
        client.set(f"control:stop_fault_test:{fault_id}", "1", ex=3600)
    if anomaly.get("edge_id"):
        edge = next((item for item in topology["edges"] if item["id"] == anomaly["edge_id"]), None)
        if edge:
            client.hset(f"edge:{edge['id']}", mapping={
                "current_flow_kw": round(float(edge.get("capacity", 0)) * 0.6, 2),
                "switch_state": "closed",
                "protective_relay_status": "NORMAL",
                "is_faulted": "false",
                "test_injected": "false",
            })
    elif anomaly.get("node_id"):
        node_id = anomaly["node_id"]
        raw = client.hgetall(f"node:{node_id}")
        load = float(raw.get("current_load_kw", raw.get("load_kw", 0)))
        client.hset(f"node:{node_id}", mapping={
            "current_load_kw": load,
            "load_kw": load,
            "voltage_pu": 1.0,
            "thd_percentage": 2.0,
            "status": "good",
            "is_faulted": "false",
            "test_injected": "false",
        })
    if fault_id:
        client.srem("faults:active", fault_id)
        client.delete(f"fault:{fault_id}")
        logger.info("Control layer resolved %s", fault_id)


def fault_feeder_scope(anomaly: dict[str, Any]) -> set[str]:
    values = [anomaly.get("edge_id", ""), *anomaly.get("affected_edge_ids", [])]
    return {match.group(1) for value in values if (match := re.search(r"/([0-9]{3})/P", str(value)))}


def faults_overlap(first: dict[str, Any], second: dict[str, Any]) -> bool:
    return bool(
        set(first.get("affected_node_ids", [])).intersection(second.get("affected_node_ids", []))
        or set(first.get("affected_edge_ids", [])).intersection(second.get("affected_edge_ids", []))
        or fault_feeder_scope(first).intersection(fault_feeder_scope(second))
    )


async def control_layer_loop() -> None:
    """Consume resolution commands and restore telemetry for the test control layer."""
    client = create_redis_client()
    while True:
        try:
            item = await asyncio.to_thread(client.blpop, "control:commands", 1)
            if item:
                _, payload = item
                apply_control_command(json.loads(payload), client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Control layer command failed")
            await broadcast({"type": "integration_error", "message": f"Control layer command failed: {exc}", "severity": "critical", "timestamp": now_iso()})
            await asyncio.sleep(1)


async def live_integration_loop() -> None:
    global active_anomaly, active_anomalies, active_solution
    try:
        client = create_redis_client()
        # Route geometry must be ready before the first telemetry broadcast;
        # otherwise the frontend receives raw two-point database segments and
        # overwrites the routed topology on the next WebSocket update.
        await asyncio.to_thread(route_edges_through_roads)
    except Exception as exc:
        logger.exception("Redis integration could not start")
        await broadcast({"type": "integration_error", "message": f"Redis telemetry integration could not start: {exc}", "severity": "critical", "timestamp": now_iso()})
        return
    previous_faults: set[str] = set()
    while True:
        try:
            client.ping()
            for edge in topology["edges"]:
                raw = client.hgetall(f"edge:{edge['id']}")
                if raw:
                    edge["flow"] = float(raw.get("current_flow_kw", edge.get("flow", 0)))
                    edge["telemetry"] = raw
                    edge["status"] = "affected" if detector_is_online(client) and str(raw.get("is_faulted", "false")).lower() == "true" else "healthy"
            for node in topology["nodes"]:
                key_id = node["id"].removeprefix("DTC_") if node["type"] == "dtc" else node["id"]
                raw = client.hgetall(f"node:{key_id}")
                if raw:
                    node["telemetry"] = raw
                    node["status"] = "affected" if detector_is_online(client) and str(raw.get("is_faulted", "false")).lower() == "true" else "healthy"
                    if raw.get("loading_percentage") is not None:
                        node["load"] = float(raw["loading_percentage"])
                    elif raw.get("current_load_kw") is not None and node.get("capacity"):
                        node["load"] = round(float(raw["current_load_kw"]) / node["capacity"] * 100, 1)
            detector_online = detector_is_online(client)
            current_faults = {str(item) for item in client.smembers("faults:active")} if detector_online else set()
            if current_faults != previous_faults:
                cleared_faults = sorted(previous_faults - current_faults)
                if cleared_faults:
                    if active_solution and active_solution.get("anomaly_id") in cleared_faults:
                        active_solution = None
                    for fault_id in cleared_faults:
                        active_anomalies.pop(fault_id, None)
                    if active_anomaly and active_anomaly.get("id") in cleared_faults:
                        active_anomaly = None
                    event = add_event("anomaly_cleared", "Detection layer cleared resolved fault(s)", "success")
                    await broadcast({**event, "cleared_anomaly_ids": cleared_faults})
                if current_faults:
                    fault_records = {fault["id"]: fault for fault in (fault_from_redis(client, fault_id) for fault_id in sorted(current_faults)) if fault}
                    active_anomalies = fault_records
                    first_fault = next(iter(fault_records.values()), None)
                    active_anomaly = first_fault
                    if first_fault:
                        await run_milp_for_active_fault()
                        for fault in fault_records.values():
                            event = add_event("anomaly_detected", fault["title"], "critical", anomaly=fault)
                            await broadcast(event)
                else:
                    active_anomaly = None
                    active_anomalies = {}
                previous_faults = current_faults
            await broadcast({"type": "telemetry_update", "timestamp": now_iso(), "nodes": topology["nodes"], "edges": topology["edges"]})
        except Exception as exc:
            logger.exception("Live Redis integration cycle failed")
            await broadcast({"type": "integration_error", "message": f"Redis telemetry error: {exc}", "severity": "critical", "timestamp": now_iso()})
        await asyncio.sleep(float(os.getenv("LIVE_POLL_SECONDS", "2")))


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(live_integration_loop())
    control_task = asyncio.create_task(control_layer_loop())
    yield
    task.cancel()
    control_task.cancel()


app = FastAPI(title="GridBandhu Topology API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.include_router(ingestion_router, prefix="/api/ingestion", tags=["telemetry-ingestion"], dependencies=[Depends(require_permission("view"))])
app.include_router(milp_router, prefix="/api/milp", tags=["milp"], dependencies=[Depends(require_permission("view"))])


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "topology_source": topology["source"], "nodes": len(topology["nodes"]), "edges": len(topology["edges"])}


@app.get("/api/diagnostics")
def diagnostics() -> dict[str, Any]:
    """Safe runtime diagnostics; never returns database credentials."""
    result: dict[str, Any] = {"api": "ok", "topology_source": topology["source"], "topology_rows": {"nodes": len(topology["nodes"]), "edges": len(topology["edges"])}, "postgres": "unknown", "redis": "unknown"}
    if psycopg2 is None:
        result["postgres"] = "driver_missing"
    else:
        conn = None
        try:
            conn = psycopg2.connect(host=os.getenv("POSTGRES_HOST", "localhost"), port=os.getenv("POSTGRES_PORT", 5432), dbname=os.getenv("POSTGRES_DB", "gridbandhu"), user=os.getenv("POSTGRES_USER", "postgres"), password=os.getenv("POSTGRES_PASSWORD", ""), connect_timeout=3)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM substations"); result["postgres_substations"] = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM ht_edges"); result["postgres_ht_edges"] = cur.fetchone()[0]
            result["postgres"] = "ok"
        except Exception as exc:
            result["postgres"] = "error"
            result["postgres_error"] = str(exc)
        finally:
            if conn is not None:
                conn.close()

    redis_conn = None
    try:
        redis_conn = create_redis_client()
        redis_conn.ping()
        result["redis"] = "ok"
        result["redis_active_faults"] = len(redis_conn.smembers("faults:active"))
    except Exception as exc:
        result["redis"] = "error"
        result["redis_error"] = str(exc)
    finally:
        if redis_conn is not None:
            redis_conn.close()
    return result


@app.post("/api/auth/login")
def login(body: LoginRequest) -> dict[str, Any]:
    if not body.username or not body.password or not credentials_match(body.username, body.password):
        raise HTTPException(401, "Invalid username or password")
    return {"token": create_token(body.username, "operator"), "user": {"name": body.username, "role": "operator"}}


@app.get("/api/topology")
def get_topology(_: dict = Depends(require_permission("view"))) -> dict[str, Any]:
    route_edges_through_roads()
    return {**topology, "anomaly": active_anomaly, "selected_solution": active_solution}


@app.get("/api/telemetry/snapshot")
def get_telemetry(_: dict = Depends(require_permission("view"))) -> dict[str, Any]:
    return {"timestamp": now_iso(), "nodes": topology["nodes"], "edges": topology["edges"]}


@app.post("/api/test/inject-fault")
async def inject_test_fault(body: TestFaultRequest) -> dict[str, Any]:
    """Inject a temporary relay trip into Redis for end-to-end testing."""
    if os.getenv("GRIDBANDHU_ENABLE_TEST_CONTROLS", "true").lower() != "true":
        raise HTTPException(403, "Test controls are disabled")
    edge = next((item for item in topology["edges"] if item["type"] == "ht" and (body.edge_id is None or str(item["id"]) == body.edge_id)), None)
    if not edge:
        raise HTTPException(404, "No matching HT edge found in the Postgres topology")
    client = create_redis_client()
    client.hset(f"edge:{edge['id']}", mapping={"current_flow_kw": 0, "switch_state": "open", "protective_relay_status": "PICKUP", "test_injected": "true"})
    return {"status": "injected", "edge_id": edge["id"], "message": "Test relay trip written to Redis; detection should publish an alert within its next cycle."}


@app.post("/api/test/clear-fault")
async def clear_test_fault(body: TestFaultRequest) -> dict[str, Any]:
    """Restore a test edge's normal Redis telemetry so the detector can clear it."""
    if os.getenv("GRIDBANDHU_ENABLE_TEST_CONTROLS", "true").lower() != "true":
        raise HTTPException(403, "Test controls are disabled")
    edge = next((item for item in topology["edges"] if item["type"] == "ht" and (body.edge_id is None or str(item["id"]) == body.edge_id)), None)
    if not edge:
        raise HTTPException(404, "No matching HT edge found in the Postgres topology")
    client = create_redis_client()
    client.hset(f"edge:{edge['id']}", mapping={"current_flow_kw": round(float(edge.get("capacity", 0)) * 0.6, 2), "switch_state": "closed", "protective_relay_status": "NORMAL", "test_injected": "false"})
    return {"status": "cleared", "edge_id": edge["id"], "message": "Normal telemetry written to Redis; detection should clear the alert within its next cycle."}


@app.get("/api/events")
def get_events(_: dict = Depends(require_permission("view"))) -> list[dict[str, Any]]:
    return events


@app.get("/api/anomalies/active")
def get_anomaly(_: dict = Depends(require_permission("view"))) -> dict[str, Any] | None:
    return active_anomaly if active_anomaly and detector_is_online(create_redis_client()) else None


@app.get("/api/anomalies/active/all")
def get_active_anomalies(_: dict = Depends(require_permission("view"))) -> list[dict[str, Any]]:
    client = create_redis_client()
    if not detector_is_online(client):
        return []
    records = []
    for fault_id in sorted(str(item) for item in client.smembers("faults:active")):
        fault = fault_from_redis(client, fault_id)
        if fault:
            records.append(fault)
    return records


def build_solution_edge_paths() -> list[list[str]]:
    fault_id = active_anomaly["edge_id"]
    fault = next((edge for edge in topology["edges"] if edge["id"] == fault_id), None)
    base = list(dict.fromkeys(active_anomaly["affected_edge_ids"]))
    neighbors = []
    if fault:
        endpoints = {fault["from"], fault["to"]}
        neighbors = [edge["id"] for edge in topology["edges"] if edge["id"] not in base and (edge["from"] in endpoints or edge["to"] in endpoints)]
    paths = [list(dict.fromkeys([fault_id, *base]))]
    paths.append(list(dict.fromkeys([fault_id, *base[1:2], *neighbors[:1]])))
    paths.append(list(dict.fromkeys([fault_id, *base[2:3], *neighbors[1:2]])))
    return paths


def route_node_to_solution_feeder(node_id: str | list[str], path: dict[str, Any]) -> list[str]:
    """Project a feeder allocation onto one connected map route.

    MILP allocations identify served feeders, not a map polyline. Use the
    topology graph to derive the shortest connected route from a faulted node
    to one feeder represented by this alternative.
    """
    allocations = [item for item in path.get("allocations", []) if item.get("status") != "SHED"]
    feeder_ids = [str(item.get("feeder_id")) for item in allocations if item.get("feeder_id")]
    if not feeder_ids:
        return []
    feeder_id = feeder_ids[(int(path.get("path_id", 1)) - 1) % len(feeder_ids)]
    target_edges = [edge for edge in topology["edges"] if f"/{feeder_id}/" in str(edge.get("id", ""))]
    if not target_edges:
        return []
    adjacency = defaultdict(list)
    for edge in topology["edges"]:
        adjacency[edge["from"]].append((edge["to"], edge["id"]))
        adjacency[edge["to"]].append((edge["from"], edge["id"]))
    targets = {endpoint for edge in target_edges for endpoint in (edge["from"], edge["to"])}
    start_nodes = [node_id] if isinstance(node_id, str) else node_id
    queue = deque((start, []) for start in start_nodes)
    visited = set(start_nodes)
    while queue:
        current, route = queue.popleft()
        if current in targets:
            target_edge = next((edge["id"] for edge in target_edges if current in {edge["from"], edge["to"]}), None)
            return list(dict.fromkeys([*route, target_edge] if target_edge else route))
        for neighbor, edge_id in adjacency[current]:
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append((neighbor, [*route, edge_id]))
    return []


def route_fault_to_solution_feeder(anomaly: dict[str, Any], path: dict[str, Any]) -> list[str]:
    """Build a local route for either a node fault or an edge fault."""
    if anomaly.get("node_id"):
        starts = [anomaly["node_id"]]
    elif anomaly.get("edge_id"):
        fault_edge = next((edge for edge in topology["edges"] if edge["id"] == anomaly["edge_id"]), None)
        starts = [fault_edge["from"], fault_edge["to"]] if fault_edge else []
    else:
        starts = []
    route = route_node_to_solution_feeder(starts, path) if starts else []
    return list(dict.fromkeys([*anomaly.get("affected_edge_ids", []), *route]))


@app.get("/api/anomalies/{anomaly_id:path}/solutions")
def get_solutions(anomaly_id: str, _: dict = Depends(require_permission("view"))) -> dict[str, Any]:
    selected_anomaly = get_runtime_anomaly(anomaly_id)
    if not selected_anomaly:
        raise HTTPException(404, "Anomaly not found")
    ids = selected_anomaly["affected_node_ids"]
    try:
        from backend.state.schema import get_paths
        paths = get_paths(anomaly_id)
    except Exception as exc:
        raise HTTPException(503, f"MILP results unavailable: {exc}") from exc
    if not paths:
        raise HTTPException(503, "MILP has not produced recommendations for this active fault yet")
    confidence = [94, 86, 78]
    solutions = []
    for index, path in enumerate(paths[:3]):
        map_edges = path.get("affected_edge_ids", [])
        if selected_anomaly.get("node_id") or selected_anomaly.get("edge_id"):
            map_edges = route_fault_to_solution_feeder(selected_anomaly, path) or list(selected_anomaly.get("affected_edge_ids", []))
        solutions.append({"id": f"SOL-{path.get('path_id', index + 1):02d}", "rank": path.get("path_id", index + 1), "title": path.get("strategy", f"MILP alternative {index + 1}"), "description": path.get("description", "MILP-generated allocation alternative"), "confidence": confidence[index] if index < len(confidence) else 70, "restored_kw": round(path.get("total_served_mw", 0) * 1000), "eta_minutes": 4 + index * 2, "risk": "low" if index == 0 else "medium", "affected_node_ids": ids, "affected_edge_ids": map_edges})
    return {"anomaly_id": anomaly_id, "solutions": solutions}


@app.post("/api/anomalies/{anomaly_id:path}/solutions/{solution_id}/preview")
async def preview_solution(anomaly_id: str, solution_id: str, _: dict = Depends(require_permission("view"))) -> dict[str, Any]:
    global active_solution
    selected_anomaly = get_runtime_anomaly(anomaly_id)
    if not selected_anomaly:
        raise HTTPException(404, "Anomaly not found")
    result = {"anomaly_id": anomaly_id, "solution_id": solution_id, "state": "preview"}
    active_solution = result
    await broadcast({"type": "solution_preview", **result})
    return result


def clear_accepted_fault(anomaly: dict[str, Any], client) -> None:
    """Restore the selected Redis asset and remove only its active fault."""
    fault_id = anomaly["id"]
    client.set(f"control:stop_fault_test:{fault_id}", "1", ex=3600)
    if anomaly.get("edge_id"):
        edge = next((item for item in topology["edges"] if item["id"] == anomaly["edge_id"]), None)
        if edge:
            client.hset(f"edge:{edge['id']}", mapping={
                "current_flow_kw": round(float(edge.get("capacity", 0)) * 0.6, 2),
                "switch_state": "closed",
                "protective_relay_status": "NORMAL",
                "is_faulted": "false",
                "test_injected": "false",
            })
    elif anomaly.get("node_id"):
        node_id = anomaly["node_id"]
        node_key = f"node:{node_id}"
        raw = client.hgetall(node_key)
        load = float(raw.get("current_load_kw", raw.get("load_kw", 0)))
        client.hset(node_key, mapping={
            "current_load_kw": load,
            "load_kw": load,
            "voltage_pu": 1.0,
            "thd_percentage": 2.0,
            "status": "good",
            "is_faulted": "false",
            "test_injected": "false",
        })
    client.srem("faults:active", fault_id)
    client.delete(f"fault:{fault_id}")


@app.post("/api/anomalies/{anomaly_id:path}/solutions/{solution_id}/accept")
async def accept_solution(anomaly_id: str, solution_id: str, _: DecisionAction | None = None, __: dict = Depends(require_permission("approve_recommendation"))) -> dict[str, Any]:
    global active_solution, active_anomaly
    client = create_redis_client()
    selected_anomaly = get_runtime_anomaly(anomaly_id, client)
    if not selected_anomaly:
        raise HTTPException(404, "Anomaly not found")
    solution = next((item for item in get_solutions(anomaly_id)["solutions"] if item["id"] == solution_id), None)
    if not solution:
        raise HTTPException(404, "Solution not found for anomaly")
    active_solution = {**solution, "anomaly_id": anomaly_id, "solution_id": solution_id, "state": "queued", "applied_at": now_iso()}
    related_anomalies = [selected_anomaly]
    for fault_id in sorted(str(item) for item in client.smembers("faults:active")):
        candidate = fault_from_redis(client, fault_id)
        if candidate and candidate["id"] != selected_anomaly["id"] and faults_overlap(selected_anomaly, candidate):
            related_anomalies.append(candidate)
    command = {
        "command_id": f"CMD-{int(time.time() * 1000)}",
        "command_type": "resolve_fault",
        "anomalies": related_anomalies,
        "solution_id": solution_id,
        "created_at": now_iso(),
    }
    client.rpush("control:commands", json.dumps(command))
    event = add_event("solution_applied", f"{solution_id} applied successfully", "success", solution=active_solution)
    await broadcast({"type": "solution_applied", "solution": active_solution, "anomaly": selected_anomaly, "control_command": command})
    await broadcast(event)
    return {"solution": active_solution, "anomaly": selected_anomaly, "control_command": command}


@app.websocket("/api/ws")
async def websocket_endpoint(socket: WebSocket, token: str | None = Query(default=None)):
    try:
        authenticate_websocket(token)
    except HTTPException:
        logger.warning(
            "Rejected unauthenticated WebSocket: path=%s origin=%s user_agent=%s token_parameter=%s",
            socket.url.path,
            socket.headers.get("origin", "-"),
            socket.headers.get("user-agent", "-")[:160],
            bool(token),
        )
        await socket.close(code=1008, reason="Authentication required")
        return
    await socket.accept()
    connections.add(socket)
    await socket.send_json({"type": "connected", "timestamp": now_iso()})
    if active_anomaly:
        await socket.send_json({"type": "anomaly_detected", "anomaly": active_anomaly, "timestamp": now_iso()})
    try:
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        connections.discard(socket)


@app.websocket("/api/v1/ws/live")
async def legacy_live_websocket_endpoint(socket: WebSocket, token: str | None = Query(default=None)):
    """Compatibility route for the existing live-data client contract."""
    await websocket_endpoint(socket, token)

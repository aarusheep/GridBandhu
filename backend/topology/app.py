"""GridBandhu API: topology, telemetry, anomaly and decision simulation."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import logging
import math
import os
import random
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

try:
    import psycopg2
except ImportError:  # The CSV demo fallback remains usable without DB packages.
    psycopg2 = None


ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "dataset2.0"
load_dotenv(ROOT / ".env")
logger = logging.getLogger("gridbandhu")


class LoginRequest(BaseModel):
    username: str
    password: str


class DecisionAction(BaseModel):
    action: str = "accept"


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
    for edge in topology["edges"]:
        if edge.get("type") not in {"ht", "dt", "lt"} or len(edge.get("coordinates", [])) < 2:
            continue
        start, end = edge["coordinates"][0], edge["coordinates"][-1]
        query = urllib.parse.urlencode({"overview": "full", "geometries": "geojson"})
        url = f"https://router.project-osrm.org/route/v1/driving/{start[0]},{start[1]};{end[0]},{end[1]}?{query}"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "GridBandhu/0.1"})
            with urllib.request.urlopen(request, timeout=2.5) as response:
                result = json.loads(response.read().decode("utf-8"))
            if result.get("routes"):
                routed = result["routes"][0]["geometry"]["coordinates"]
                route_length = sum(distance_km(routed[index - 1], routed[index]) for index in range(1, len(routed)))
                direct_length = distance_km(start, end)
                if route_length <= max(direct_length * 6, direct_length + 1.0):
                    edge["coordinates"] = [start, *routed[1:-1], end]
                    edge["road_route_km"] = round(route_length, 3)
                    edge["road_direct_km"] = round(direct_length, 3)
                    edge["road_routed"] = True
                else:
                    edge["road_routed"] = False
                    edge["routing_error"] = f"Rejected excessive detour ({route_length:.1f} km for {direct_length:.1f} km direct)"
        except Exception:
            edge["road_routed"] = False
            edge["routing_error"] = "OSRM route unavailable; inspect backend logs"
            edge["coordinates"] = [start, end]
            logger.warning("Road routing failed for %s", edge.get("id"), exc_info=True)
    roads_loaded = True
events: list[dict[str, Any]] = []
connections: set[WebSocket] = set()
active_anomaly: dict[str, Any] | None = None
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


async def demo_detection_loop() -> None:
    global active_anomaly
    await asyncio.sleep(3)
    while True:
        if active_anomaly is None:
            affected = [n for n in topology["nodes"] if n["type"] == "dtc"][:2]
            affected_ids = [n["id"] for n in affected]
            affected_edges = [e["id"] for e in topology["edges"] if e["from"] in affected_ids or e["to"] in affected_ids][:3]
            active_anomaly = {"id": "ANOM-001", "type": "overload", "severity": "critical", "title": "Transformer overload detected", "reason": "Loading exceeded safe operating threshold", "source_node_id": affected_ids[0] if affected_ids else "DTC_UNKNOWN", "affected_node_ids": affected_ids, "affected_edge_ids": affected_edges, "detected_at": now_iso(), "status": "open"}
            event = add_event("anomaly_detected", "Transformer overload detected", "critical", anomaly=active_anomaly)
            await broadcast(event)
        for node in topology["nodes"]:
            if node["type"] in {"dtc", "facility"}:
                node["load"] = round(max(20, min(96, node["load"] + random.uniform(-2.2, 2.2))), 1)
        await broadcast({"type": "telemetry_update", "timestamp": now_iso(), "nodes": topology["nodes"], "edges": topology["edges"]})
        await asyncio.sleep(8)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(demo_detection_loop())
    yield
    task.cancel()


app = FastAPI(title="GridBandhu Topology API", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "topology_source": topology["source"], "nodes": len(topology["nodes"]), "edges": len(topology["edges"])}


@app.get("/api/diagnostics")
def diagnostics() -> dict[str, Any]:
    """Safe runtime diagnostics; never returns database credentials."""
    result: dict[str, Any] = {"api": "ok", "topology_source": topology["source"], "topology_rows": {"nodes": len(topology["nodes"]), "edges": len(topology["edges"])}, "postgres": "unknown", "redis": "not_required_by_demo_loop"}
    if psycopg2 is None:
        result["postgres"] = "driver_missing"
        return result
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
    return result


@app.post("/api/auth/login")
def login(body: LoginRequest) -> dict[str, Any]:
    if not body.username or not body.password:
        raise HTTPException(401, "Username and password are required")
    return {"token": "demo-session", "user": {"name": body.username, "role": "Grid Operations"}}


@app.get("/api/topology")
def get_topology() -> dict[str, Any]:
    route_edges_through_roads()
    return {**topology, "anomaly": active_anomaly, "selected_solution": active_solution}


@app.get("/api/telemetry/snapshot")
def get_telemetry() -> dict[str, Any]:
    return {"timestamp": now_iso(), "nodes": topology["nodes"], "edges": topology["edges"]}


@app.get("/api/events")
def get_events() -> list[dict[str, Any]]:
    return events


@app.get("/api/anomalies/active")
def get_anomaly() -> dict[str, Any] | None:
    return active_anomaly


@app.get("/api/anomalies/{anomaly_id}/solutions")
def get_solutions(anomaly_id: str) -> dict[str, Any]:
    if not active_anomaly or anomaly_id != active_anomaly["id"]:
        raise HTTPException(404, "Anomaly not found")
    ids = active_anomaly["affected_node_ids"]
    return {"anomaly_id": anomaly_id, "solutions": [{"id": "SOL-01", "rank": 1, "title": "Transfer load to alternate feeder", "description": "Open the affected section and route supply through the nearest healthy tie switch.", "confidence": 94, "restored_kw": 680, "eta_minutes": 4, "risk": "low", "affected_node_ids": ids, "affected_edge_ids": active_anomaly["affected_edge_ids"]}, {"id": "SOL-02", "rank": 2, "title": "Redistribute transformer demand", "description": "Balance demand across adjacent DTCs while preserving critical facilities.", "confidence": 86, "restored_kw": 540, "eta_minutes": 7, "risk": "medium", "affected_node_ids": ids[:1], "affected_edge_ids": active_anomaly["affected_edge_ids"][:2]}, {"id": "SOL-03", "rank": 3, "title": "Prioritize critical consumers", "description": "Curtail non-critical load to keep hospital and water services online.", "confidence": 78, "restored_kw": 390, "eta_minutes": 2, "risk": "medium", "affected_node_ids": ids, "affected_edge_ids": active_anomaly["affected_edge_ids"][:1]}]}


@app.post("/api/anomalies/{anomaly_id}/solutions/{solution_id}/preview")
async def preview_solution(anomaly_id: str, solution_id: str) -> dict[str, Any]:
    global active_solution
    if not active_anomaly or anomaly_id != active_anomaly["id"]:
        raise HTTPException(404, "Anomaly not found")
    result = {"anomaly_id": anomaly_id, "solution_id": solution_id, "state": "preview"}
    active_solution = result
    await broadcast({"type": "solution_preview", **result})
    return result


@app.post("/api/anomalies/{anomaly_id}/solutions/{solution_id}/accept")
async def accept_solution(anomaly_id: str, solution_id: str, _: DecisionAction | None = None) -> dict[str, Any]:
    global active_solution, active_anomaly
    if not active_anomaly or anomaly_id != active_anomaly["id"]:
        raise HTTPException(404, "Anomaly not found")
    active_solution = {"anomaly_id": anomaly_id, "solution_id": solution_id, "state": "applied", "applied_at": now_iso()}
    active_anomaly = {**active_anomaly, "status": "resolved", "resolved_at": now_iso()}
    for node in topology["nodes"]:
        if node["id"] in active_anomaly["affected_node_ids"]:
            node["status"] = "restored"
            node["load"] = min(node["load"], 72)
    event = add_event("solution_applied", f"{solution_id} applied successfully", "success", solution=active_solution)
    await broadcast({"type": "solution_applied", "solution": active_solution, "anomaly": active_anomaly})
    await broadcast(event)
    return {"solution": active_solution, "anomaly": active_anomaly}


@app.websocket("/api/ws")
async def websocket_endpoint(socket: WebSocket):
    await socket.accept()
    connections.add(socket)
    await socket.send_json({"type": "connected", "timestamp": now_iso()})
    try:
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        connections.discard(socket)


@app.websocket("/api/v1/ws/live")
async def legacy_live_websocket_endpoint(socket: WebSocket):
    """Compatibility route for the existing live-data client contract."""
    await websocket_endpoint(socket)

# GridBandhu

GridBandhu is a real-time grid monitoring and fault-recovery decision-support system. It continuously ingests electrical network telemetry, detects faults as they occur, computes optimal power-reallocation strategies, and presents both on an interactive topology map for operator review and action.

The system is organized into four functional layers — data ingestion, topology, fault detection, and optimization (MILP) — backed by a hybrid data store: PostgreSQL/PostGIS for fixed network structure, and Redis for live, fast-changing telemetry and system state.

---

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Repository Structure](#repository-structure)
3. [Technology Stack](#technology-stack)
4. [Data Storage Design](#data-storage-design)
5. [Prerequisites](#prerequisites)
6. [Environment Configuration](#environment-configuration)
7. [Installation](#installation)
8. [Running the System](#running-the-system)
9. [API Overview](#api-overview)
10. [Security](#security)
11. [Contributors](#contributors)

---

## System Architecture

The system is designed around a strict separation of responsibilities: each layer produces data for the next layer to consume, and no layer re-derives work that a previous layer has already done.

```
                 ┌─────────────────────────┐
                 │   Data Ingestion Layer   │
                 │  (static + live sources) │
                 └────────────┬─────────────┘
                              │
                 ┌────────────▼─────────────┐
                 │   PostgreSQL / PostGIS    │      Redis
                 │  (fixed network topology) │◄────►(live telemetry,
                 └────────────┬─────────────┘       fault state,
                              │                      MILP output)
                 ┌────────────▼─────────────┐
                 │      Topology Layer       │
                 │   (FastAPI application,   │
                 │  map + WebSocket serving) │
                 └────────────┬─────────────┘
                              │
                 ┌────────────▼─────────────┐
                 │      Detection Layer      │
                 │  (fault checks, writes    │
                 │   faults:active in Redis) │
                 └────────────┬─────────────┘
                              │
                 ┌────────────▼─────────────┐
                 │        MILP Layer         │
                 │ (reallocation solver, top │
                 │  3 recovery paths)        │
                 └────────────┬─────────────┘
                              │
                 ┌────────────▼─────────────┐
                 │         Frontend          │
                 │  (map visualization of    │
                 │   faults and MILP paths)  │
                 └───────────────────────────┘
```

**Responsibility of each layer:**

| Layer | Responsibility |
|---|---|
| Data Ingestion | Loads fixed network structure into PostgreSQL once; continuously writes live telemetry readings into Redis; validates and accepts external telemetry submissions over HTTP. |
| Topology | Serves the network topology and live state to the frontend over a FastAPI application, including a WebSocket feed for real-time map updates. |
| Detection | Polls live telemetry against structural rules and flags faults; writes fault records and an active-fault set into Redis. |
| MILP | Triggered only when a fault is active; computes the top three power-reallocation strategies and writes them into Redis for the map to display. |
| Frontend | Reads topology, live telemetry, active faults, and MILP paths, and renders all of it on an interactive map. Performs no computation of its own. |

---

## Repository Structure

```
GridBandhu/
├── backend/
│   ├── data_ingestion_layer/
│   │   ├── static_data.py        # One-time load of fixed topology into PostgreSQL/PostGIS
│   │   ├── live_data.py          # Continuous live telemetry generator (writes to Redis)
│   │   ├── faulty_live_data.py   # Telemetry generator with a deliberately induced fault, for testing
│   │   ├── ingestion_api.py      # HTTP endpoint for external/validated telemetry submissions
│   │   └── validation.py         # Pydantic validation rules for incoming telemetry payloads
│   │
│   ├── topology/
│   │   ├── app.py                # FastAPI application: topology API, WebSocket feed, background loops
│   │   └── __init__.py
│   │
│   ├── Detection/
│   │   └── detection.py          # Fault detection loop (full outage, overload, protection trip)
│   │
│   ├── milp/
│   │   ├── formulation.py        # MILP model definition (constraints, objective)
│   │   ├── solver.py             # Solves the model, returns top 3 ranked allocations
│   │   ├── scheduler.py          # Polls faults:active and triggers the solver
│   │   ├── diff.py               # Compares successive allocations and generates reason codes
│   │   ├── weights.py            # Feeder dependency and priority configuration
│   │   ├── grid.py               # FastAPI router exposing MILP results
│   │   └── quick_test.py         # Manual solver smoke test
│   │
│   ├── state/
│   │   └── schema.py             # Redis/PostGIS read-write bridge used by the MILP layer
│   │
│   ├── security/
│   │   ├── auth.py               # JWT authentication
│   │   ├── rbac.py                # Role-based access control (admin / operator / viewer)
│   │   ├── encryption.py          # AES-GCM encryption for telemetry payloads
│   │   └── telemetry.py           # Freshness/replay checks for incoming telemetry
│   │
│   ├── dataset2.0                # Source dataset used to seed static topology data
│   └── __init__.py
│
├── frontend/
│   ├── src/                      # React application (map rendering, live dashboard)
│   ├── public/
│   ├── package.json
│   └── vite.config.js
│
├── .env.example                  # Template for required environment variables
├── requirements.txt               # Python dependencies
├── ACTIVATION.md                  # Virtual environment activation reference
└── README.md
```

---

## Technology Stack

**Backend**
- Python 3.11+
- FastAPI — API layer and WebSocket serving
- PostgreSQL with PostGIS — persistent network topology and geospatial data
- Redis — live telemetry, fault state, and MILP output
- PuLP — MILP formulation and solving
- APScheduler — periodic execution of the detection loop and telemetry generator
- Pydantic — request/payload validation
- python-jose, cryptography — authentication and payload encryption

**Frontend**
- React 19
- Vite
- MapLibre GL — interactive map rendering

---

## Data Storage Design

The system uses PostgreSQL and Redis for distinct purposes, and does not duplicate data between them.

**PostgreSQL / PostGIS** stores facts that change rarely: facility, substation, HT pole, and DTC identities and coordinates; line capacities and connections; consumer-to-DTC mappings; billing/revenue groupings; and a precomputed parent chain on each feeder record (DTC → HT pole → HT line → substation) used to construct fault-path descriptions without a live network trace.

**Redis** stores state that changes continuously: current load and flow per node/edge, switch and relay status, the active-fault set (`faults:active`), individual fault records (`fault:{fault_id}`), and MILP's top-3 recovery paths (`milp:paths:{fault_id}`).

The map and API layers read from both stores and join them at request time; nothing is cached as a persistent copy across the two systems.

---

## Prerequisites

- Python 3.11 or later
- Node.js 18 or later (with npm or pnpm)
- PostgreSQL 14+ with the PostGIS extension enabled
- Redis 6+

---

## Environment Configuration

Copy the provided template and populate it with real credentials before running any component:

```bash
cp .env.example backend/.env
```

Required variables (see `.env.example`):

| Variable | Purpose |
|---|---|
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | PostgreSQL connection |
| `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB` | Redis connection |
| `JWT_SECRET`, `JWT_TTL_MINUTES` | Authentication token signing and expiry |
| `ENCRYPTION_KEY` | AES-GCM key for encrypted telemetry ingestion |
| `GRIDBANDHU_OPERATOR_USERNAME`, `GRIDBANDHU_OPERATOR_PASSWORD` | Default operator credentials |

All placeholder values in `.env.example` must be replaced before deployment. None of the example values are suitable for production use.

---

## Installation

### Backend

```bash
# From the repository root
python -m venv .venv

# Activate the environment (see ACTIVATION.md for platform-specific commands)
source .venv/bin/activate        # Unix/macOS
.\.venv\Scripts\Activate.ps1     # Windows PowerShell

pip install -r requirements.txt
```

### Frontend

```bash
cd frontend
npm install
```

---

## Running the System

Components are run as independent processes and should be started in the following order.

**1. Load static topology data (one-time, or whenever the source dataset changes):**

```bash
python -m backend.data_ingestion_layer.static_data
```

**2. Start the live telemetry generator:**

```bash
python -m backend.data_ingestion_layer.live_data
```

To exercise fault detection end-to-end with a deliberately induced fault instead:

```bash
python -m backend.data_ingestion_layer.faulty_live_data
```

**3. Start the detection layer:**

```bash
python -m backend.Detection.detection
```

**4. Start the topology API (serves the map, telemetry WebSocket, ingestion, and MILP endpoints):**

```bash
uvicorn backend.topology.app:app --reload
```

**5. Start the frontend:**

```bash
cd frontend
npm run dev
```

The MILP layer is triggered automatically from within the topology API's background loop whenever an active fault is present in Redis; it does not need to be started as a separate process.

---

## API Overview

The topology API exposes the following route groups, each protected by role-based permissions:

| Route prefix | Purpose |
|---|---|
| `/api/ingestion` | Accepts and validates external telemetry submissions |
| `/api/milp` | Returns computed reconfiguration paths for a given fault |
| WebSocket feed | Broadcasts live topology, telemetry, and fault/anomaly events to connected clients |

Access to all routes requires a valid JWT bearer token, issued against the credentials configured in the environment file.

---

## Security

- **Authentication:** JWT-based, with configurable token expiry.
- **Authorization:** Role-based access control with three roles — `admin`, `operator`, and `viewer` — each scoped to a defined set of permissions.
- **Transport-level protection:** Telemetry payloads submitted over HTTP are encrypted (AES-GCM) and checked for freshness to reject stale or replayed submissions.

Default credentials and secrets provided in `.env.example` are for local development only and must be changed prior to any staging or production deployment.

---

## Contributors

Team Peakachu — Smart India Hackathon 2026.

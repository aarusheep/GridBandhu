"""HTTP ingestion boundary for validated DTC telemetry."""

import os
import json
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote

import psycopg2
import redis
from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, Field

from .validation import TelemetryPayload, validate_against_topology
from backend.security.encryption import decrypt_payload
from backend.security.telemetry import require_fresh

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
if not ENV_FILE.is_file():
    raise RuntimeError(f"Required environment file not found: {ENV_FILE}")
load_dotenv(ENV_FILE)

app = FastAPI(title="GridBandhu Telemetry Ingestion")
router = APIRouter()


def get_postgres_connection():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "gridbandhu"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=unquote(os.getenv("POSTGRES_PASSWORD", "")),
    )


def get_redis_client():
    return redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        decode_responses=True,
        protocol=2,
    )


class TopologyCache:
    """Read-only in-memory cache of the authoritative DTC topology."""

    def __init__(self, loader=get_postgres_connection):
        self._loader = loader
        self._capacities: dict[str, float] = {}

    def refresh(self) -> None:
        with closing(self._loader()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT dtc_id, capacity_kw FROM dtc")
                self._capacities = {
                    str(dtc_id): float(capacity_kw)
                    for dtc_id, capacity_kw in cursor.fetchall()
                    if dtc_id is not None and capacity_kw is not None
                }

    def capacity_for(self, dtc_id: str) -> float | None:
        if not self._capacities:
            self.refresh()
        return self._capacities.get(dtc_id)


topology_cache = TopologyCache()


class EncryptedTelemetryPayload(BaseModel):
    nonce: str = Field(min_length=1)
    ciphertext: str = Field(min_length=1)


def ensure_history_table(connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS telemetry_history (
                id BIGSERIAL PRIMARY KEY,
                dtc_id TEXT NOT NULL REFERENCES dtc(dtc_id),
                load_kw DOUBLE PRECISION NOT NULL,
                capacity_kw DOUBLE PRECISION NOT NULL,
                loading_percentage DOUBLE PRECISION NOT NULL,
                voltage_pu DOUBLE PRECISION NOT NULL,
                thd_percentage DOUBLE PRECISION NOT NULL,
                timestamp TIMESTAMPTZ NOT NULL,
                ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cursor.execute(
            "ALTER TABLE telemetry_history "
            "ADD COLUMN IF NOT EXISTS voltage_pu DOUBLE PRECISION"
        )
        cursor.execute(
            "ALTER TABLE telemetry_history "
            "ADD COLUMN IF NOT EXISTS thd_percentage DOUBLE PRECISION"
        )


def persist_telemetry(payload: TelemetryPayload, redis_client=None, connection=None) -> None:
    redis_client = redis_client or get_redis_client()
    own_connection = connection is None
    connection = connection or get_postgres_connection()

    try:
        ensure_history_table(connection)
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO telemetry_history
                    (dtc_id, load_kw, capacity_kw, loading_percentage,
                     voltage_pu, thd_percentage, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    payload.dtc_id,
                    payload.load_kw,
                    payload.capacity_kw,
                    payload.loading_percentage,
                    payload.voltage_pu,
                    payload.thd_percentage,
                    payload.timestamp,
                ),
            )
        connection.commit()
        redis_client.hset(
            f"node:{payload.dtc_id}",
            mapping={
                "current_load_kw": payload.load_kw,
                "load_kw": payload.load_kw,
                "capacity_kw": payload.capacity_kw,
                "loading_percentage": payload.loading_percentage,
                "voltage_pu": payload.voltage_pu,
                "thd_percentage": payload.thd_percentage,
                "timestamp": payload.timestamp.isoformat(),
                "status": "valid",
            },
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        if own_connection:
            connection.close()


@router.post("/telemetry", status_code=status.HTTP_201_CREATED)
def ingest_telemetry(payload: TelemetryPayload, request: Request):
    topology = getattr(request.app.state, "topology_cache", topology_cache)
    redis_client = getattr(request.app.state, "redis_client", None)
    connection = getattr(request.app.state, "postgres_connection", None)

    try:
        require_fresh(payload.timestamp)
        capacity = topology.capacity_for(payload.dtc_id)
        validate_against_topology(payload, capacity)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    persist_telemetry(payload, redis_client=redis_client, connection=connection)
    return {"status": "accepted", "dtc_id": payload.dtc_id}


@router.post("/telemetry/encrypted", status_code=status.HTTP_201_CREATED)
def ingest_encrypted_telemetry(envelope: EncryptedTelemetryPayload, request: Request):
    """Decrypt and authenticate telemetry before applying normal validation."""
    try:
        payload = TelemetryPayload.model_validate(json.loads(decrypt_payload(envelope.nonce, envelope.ciphertext)))
    except (ValueError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return ingest_telemetry(payload, request)


app.include_router(router)

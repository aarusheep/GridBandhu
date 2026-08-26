"""Validation rules for incoming DTC grid telemetry."""

from datetime import datetime, timedelta, timezone
from math import isclose, isfinite

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class TelemetryPayload(BaseModel):
    """Wire contract for one DTC telemetry reading."""

    model_config = ConfigDict(extra="forbid")

    dtc_id: StrictStr = Field(min_length=1)
    load_kw: float
    capacity_kw: float
    loading_percentage: float
    voltage_pu: float
    thd_percentage: float
    timestamp: datetime

    @field_validator(
        "load_kw",
        "capacity_kw",
        "loading_percentage",
        "voltage_pu",
        "thd_percentage",
        mode="before",
    )
    @classmethod
    def require_finite_numbers(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("numeric values must be JSON numbers")
        if not isfinite(value):
            raise ValueError("numeric values must be finite")
        return value

    @field_validator("timestamp")
    @classmethod
    def require_recent_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")

        now = datetime.now(timezone.utc)
        timestamp = value.astimezone(timezone.utc)
        if abs(now - timestamp) > timedelta(minutes=5):
            raise ValueError("timestamp drift exceeds 5 minutes")
        return timestamp


def validate_against_topology(
    payload: TelemetryPayload,
    topology_capacity_kw: float | None,
) -> None:
    """Apply referential and domain rules after Pydantic parsing."""

    if topology_capacity_kw is None:
        raise LookupError(f"unknown dtc_id: {payload.dtc_id}")

    if payload.load_kw < 0 or payload.capacity_kw <= 0 or topology_capacity_kw <= 0:
        raise ValueError("load_kw and capacity_kw must be within valid bounds")

    if not isclose(payload.capacity_kw, topology_capacity_kw, rel_tol=0, abs_tol=0.01):
        raise ValueError("capacity_kw does not match static topology")

    if payload.load_kw > 1.5 * topology_capacity_kw:
        raise ValueError("load_kw exceeds 1.5 times capacity_kw")

    expected_percentage = (payload.load_kw / topology_capacity_kw) * 100
    if not isclose(payload.loading_percentage, expected_percentage, rel_tol=0, abs_tol=0.1):
        raise ValueError("loading_percentage does not match load_kw / capacity_kw")

    if not 0.88 <= payload.voltage_pu <= 1.10:
        raise ValueError("voltage_pu must be between 0.88 and 1.10")

    if not 0 <= payload.thd_percentage <= 5.0:
        raise ValueError("thd_percentage must be between 0 and 5.0")
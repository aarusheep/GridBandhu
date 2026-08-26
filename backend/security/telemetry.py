"""Security envelope for telemetry submitted over HTTP."""

from datetime import datetime, timedelta, timezone


MAX_AGE_SECONDS = 90


def require_fresh(timestamp: datetime) -> None:
    timestamp = timestamp.astimezone(timezone.utc)
    if datetime.now(timezone.utc) - timestamp > timedelta(seconds=MAX_AGE_SECONDS):
        raise ValueError("telemetry is stale and was rejected")

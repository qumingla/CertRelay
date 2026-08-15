from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .timeutil import parse_iso, to_iso, utc_now


def node_presence(
    row: dict[str, Any],
    *,
    heartbeat_interval_seconds: int,
    missed_heartbeats: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    last_heartbeat = parse_iso(row.get("last_heartbeat_at"))
    if last_heartbeat is None:
        return {"isOnline": False, "offlineAt": None}

    threshold_seconds = max(1, heartbeat_interval_seconds) * max(1, missed_heartbeats)
    offline_at = last_heartbeat + timedelta(seconds=threshold_seconds)
    current_time = now or utc_now()
    is_online = current_time < offline_at
    return {
        "isOnline": is_online,
        "offlineAt": None if is_online else to_iso(offline_at),
    }

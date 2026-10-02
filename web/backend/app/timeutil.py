from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return to_iso(utc_now())


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def days_remaining(expires_at: str | None) -> int | None:
    parsed = parse_iso(expires_at)
    if parsed is None:
        return None
    delta = parsed - utc_now()
    return int(delta.total_seconds() // 86400)

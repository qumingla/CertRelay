from __future__ import annotations

import logging
import time
from typing import Any
from uuid import uuid4

from .db import Database
from .events import EventHub
from .timeutil import iso_now


logger = logging.getLogger("ssl_sync.jobs")


def create_job(
    db: Database,
    event_hub: EventHub,
    job_type: str,
    target_id: str,
    target_name: str | None = None,
    first_log: str | None = None,
) -> dict[str, Any]:
    now = iso_now()
    job_id = f"job_{uuid4().hex}"
    log = first_log or f"[INFO] Created {job_type} job for {target_name or target_id}\n"
    db.execute(
        """
        INSERT INTO jobs
            (id, type, target_id, target_name, status, started_at, ended_at, duration_ms, error, log_text, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?)
        """,
        (job_id, job_type, target_id, target_name, "running", now, log, now, now),
    )
    event_hub.publish(
        "job_started",
        "info",
        f"Started {job_type} for {target_name or target_id}",
        {"jobId": job_id, "targetId": target_id, "type": job_type},
    )
    return get_job(db, job_id)


def append_log(db: Database, job_id: str, line: str) -> None:
    row = db.query_one("SELECT log_text FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        return
    log_text = row.get("log_text") or ""
    db.execute(
        "UPDATE jobs SET log_text = ?, updated_at = ? WHERE id = ?",
        (f"{log_text}{line.rstrip()}\n", iso_now(), job_id),
    )
    message = line.rstrip()
    level = logging.ERROR if "[ERROR]" in message or "[FATAL]" in message else logging.WARNING if "[WARN]" in message else logging.INFO
    logger.log(level, "job_id=%s %s", job_id, message)


def finish_job(
    db: Database,
    event_hub: EventHub,
    job_id: str,
    status: str = "success",
    error: str | None = None,
) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise ValueError(f"Job not found: {job_id}")
    started_at = row.get("started_at")
    duration_ms = None
    if started_at:
        try:
            started_ts = time.mktime(time.strptime(started_at[:19], "%Y-%m-%dT%H:%M:%S"))
            duration_ms = int((time.time() - started_ts) * 1000)
        except ValueError:
            duration_ms = 0
    ended_at = iso_now()
    append_log(db, job_id, f"[{'ERROR' if status == 'failed' else 'INFO'}] Job finished with status: {status}")
    db.execute(
        """
        UPDATE jobs
        SET status = ?, ended_at = ?, duration_ms = ?, error = ?, updated_at = ?
        WHERE id = ?
        """,
        (status, ended_at, duration_ms, error, ended_at, job_id),
    )
    event_hub.publish(
        "job_finished",
        "error" if status == "failed" else "success",
        f"Job {job_id} finished with status {status}",
        {"jobId": job_id, "status": status},
    )
    return get_job(db, job_id)


def get_job(db: Database, job_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise KeyError(job_id)
    return job_from_row(row)


def job_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "targetId": row["target_id"],
        "targetName": row.get("target_name"),
        "status": row["status"],
        "startedAt": row.get("started_at"),
        "endedAt": row.get("ended_at"),
        "durationMs": row.get("duration_ms"),
        "error": row.get("error"),
    }

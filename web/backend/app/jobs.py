from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from uuid import uuid4

from .db import Database
from .events import EventHub
from .timeutil import iso_now, parse_iso, utc_now


logger = logging.getLogger("ssl_sync.jobs")

NODE_COMMAND_TIMEOUT = timedelta(minutes=30)
STALE_OPERATION_TIMEOUT = timedelta(minutes=35)


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
    db.execute(
        "UPDATE jobs SET log_text = COALESCE(log_text, '') || ?, updated_at = ? WHERE id = ?",
        (line.rstrip() + "\n", iso_now(), job_id),
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
            started = parse_iso(started_at)
            duration_ms = max(0, int((utc_now() - started).total_seconds() * 1000)) if started else 0
        except ValueError:
            duration_ms = 0
    ended_at = iso_now()
    message = f"[{'ERROR' if status == 'failed' else 'INFO'}] Job finished with status: {status}"
    if error:
        message += f" — {error}"
    # Timeout recovery and a node acknowledgement can arrive concurrently.
    # Only the first terminal result wins, including across API workers.
    with db.connect() as conn:
        changed = conn.execute("""
        UPDATE jobs
        SET status = ?, ended_at = ?, duration_ms = ?, error = ?, updated_at = ?,
            log_text = COALESCE(log_text, '') || ?
        WHERE id = ? AND status = 'running'
        """,
        (status, ended_at, duration_ms, error, ended_at, message + "\n", job_id)).rowcount
    if not changed:
        return get_job(db, job_id)
    logger.log(logging.ERROR if status == "failed" else logging.INFO, "job_id=%s %s", job_id, message)
    event_hub.publish(
        "job_finished",
        "error" if status == "failed" else "success",
        f"Job {job_id} finished with status {status}",
        {"jobId": job_id, "status": status},
    )
    return get_job(db, job_id)


def recover_stale_jobs(db: Database, event_hub: EventHub) -> None:
    """Expire commands, reconcile lost acknowledgements, and close orphan jobs."""
    now = utc_now()
    for command in db.query_all("SELECT * FROM node_commands WHERE status = 'pending'"):
        created = parse_iso(command["created_at"])
        if created and now - created < NODE_COMMAND_TIMEOUT:
            continue
        error = "节点命令超时：排队后 30 分钟内未收到完成回执，可能节点离线或执行中断。"
        ended = iso_now()
        with db.connect() as conn:
            changed = conn.execute("""
                UPDATE node_commands SET status = 'failed', completed_at = ?,
                    last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'pending'
            """, (ended, error, ended, command["id"])).rowcount
        if changed and command.get("job_id"):
            finish_job(db, event_hub, command["job_id"], "failed", error)

    for job in db.query_all("SELECT * FROM jobs WHERE status = 'running'"):
        if job["type"] in {"deploy", "delete", "upgrade"}:
            command = db.query_one(
                "SELECT status, last_error FROM node_commands WHERE job_id = ? ORDER BY created_at DESC LIMIT 1",
                (job["id"],),
            )
            if command and command["status"] in {"completed", "failed"}:
                finish_job(db, event_hub, job["id"],
                           "success" if command["status"] == "completed" else "failed",
                           command.get("last_error"))
            elif not command:
                started = parse_iso(job.get("started_at")) or parse_iso(job.get("created_at"))
                if not started or now - started >= NODE_COMMAND_TIMEOUT:
                    finish_job(db, event_hub, job["id"], "failed",
                               "任务超时：关联的节点命令已丢失，无法确认执行结果。")
        else:
            updated = parse_iso(job.get("updated_at")) or parse_iso(job.get("created_at"))
            if not updated or now - updated >= STALE_OPERATION_TIMEOUT:
                finish_job(db, event_hub, job["id"], "failed",
                           "任务超时：超过 35 分钟未报告进度，可能执行中断或服务重启。")


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

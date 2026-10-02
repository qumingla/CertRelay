"""Reconcile enabled certificates and node assignments without external cron."""
from __future__ import annotations

import asyncio
import fcntl
import logging
from datetime import timedelta

from .db import merged_settings
from .jobs import append_log, create_job, finish_job, recover_stale_jobs
from .live_ops import cleanup_bundle, mark_domain_error, run_domain_script, update_domain_state
from .timeutil import iso_now, parse_iso, utc_now

logger = logging.getLogger("ssl_sync.scheduler")


async def reconcile(app) -> None:
    db, hub = app.state.db, app.state.event_hub
    recover_stale_jobs(db, hub)
    settings_row = db.query_one("SELECT value FROM app_settings WHERE key = 'settings'")
    settings = merged_settings(settings_row["value"] if settings_row else None)
    renew_days = int(settings["acme"]["defaultRenewDays"])
    for row in db.query_all("SELECT * FROM domains WHERE enabled = 1 ORDER BY domain"):
        expiry = parse_iso(row.get("expires_at"))
        if expiry and expiry > utc_now() + timedelta(days=renew_days) and row.get("cert_sha256"):
            continue
        # ACME's account/config files are shared with manual and bulk operations.
        if db.query_one("SELECT id FROM jobs WHERE status = 'running' AND type IN ('issue', 'renew', 'sync', 'test_dns') LIMIT 1"):
            break
        recent = db.query_one("SELECT started_at FROM jobs WHERE target_id = ? AND type IN ('issue', 'renew') ORDER BY created_at DESC LIMIT 1", (row["id"],))
        last_attempt = parse_iso(recent["started_at"]) if recent else None
        if last_attempt and last_attempt > utc_now() - timedelta(hours=1):
            continue
        job = create_job(db, hub, "renew", row["id"], row["domain"], "[INFO] Automatic certificate renewal\n")
        bundle = None
        try:
            # The configured expiry threshold, rather than acme.sh's own schedule,
            # decides when renewal is due.
            bundle = await run_domain_script(app.state.config, db, row["id"], force_reissue=True,
                                             line_logger=lambda line: append_log(db, job["id"], line))
            update_domain_state(db, row["id"], bundle, mark_issued=True, mark_synced=True)
            finish_job(db, hub, job["id"])
        except asyncio.CancelledError:
            finish_job(db, hub, job["id"], "failed", "Service stopped during renewal")
            raise
        except Exception as exc:
            mark_domain_error(db, row["id"], str(exc))
            append_log(db, job["id"], f"[ERROR] {exc}")
            finish_job(db, hub, job["id"], "failed", str(exc))
        finally:
            cleanup_bundle(bundle)
    queue_deployments(app)


def queue_deployments(app) -> None:
    from .routers.admin import _queue_node_command

    db = app.state.db
    rows = db.query_all("""
        SELECT a.node_id, a.domain_id FROM node_assignments a
        JOIN domains d ON d.id = a.domain_id
        WHERE d.enabled = 1 AND a.desired_sha256 IS NOT NULL
          AND (COALESCE(a.deployed_sha256, '') <> a.desired_sha256 OR a.status IN ('pending', 'error'))
        ORDER BY a.node_id
    """)
    grouped: dict[str, list[str]] = {}
    for row in rows:
        grouped.setdefault(row["node_id"], []).append(row["domain_id"])
    for node_id, ids in grouped.items():
        # Leave offline-node commands pending; retry failures after a cooldown.
        if db.query_one("SELECT id FROM node_commands WHERE node_id = ? AND status = 'pending' LIMIT 1", (node_id,)):
            continue
        # Explicit deletion pauses automatic deployment for these assignments until
        # a new certificate is issued or the user requests deployment again.
        deleted = db.query_all("SELECT payload_json, completed_at FROM node_commands WHERE node_id = ? AND type = 'delete_domains' AND status = 'completed'", (node_id,))
        from .db import loads_object
        for command in deleted:
            for domain_id in loads_object(command["payload_json"]).get("domainIds", []):
                domain = db.query_one("SELECT last_issued_at FROM domains WHERE id = ?", (domain_id,))
                issued = parse_iso(domain.get("last_issued_at")) if domain else None
                completed = parse_iso(command["completed_at"])
                if domain_id in ids and completed and (not issued or issued <= completed):
                    ids.remove(domain_id)
        if not ids:
            continue
        latest = db.query_one("SELECT updated_at, status FROM node_commands WHERE node_id = ? ORDER BY created_at DESC LIMIT 1", (node_id,))
        last = parse_iso(latest["updated_at"]) if latest else None
        if latest and latest["status"] == "failed" and last and last > utc_now() - timedelta(minutes=5):
            continue
        _queue_node_command(db, app.state.event_hub, node_id, "sync_domains", ids)


async def run_scheduler(app) -> None:
    # One owner per database, including multi-worker deployments.
    with open(str(app.state.config.db_path) + ".scheduler.lock", "a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                await asyncio.sleep(30)
                continue
            break
        renewal = None
        try:
            while True:
                try:
                    app.state.scheduler_last_check = iso_now()
                    # Run the watchdog even while ACME renewal is still busy.
                    recover_stale_jobs(app.state.db, app.state.event_hub)
                    queue_deployments(app)
                    if renewal is None or renewal.done():
                        if renewal is not None:
                            try:
                                renewal.result()
                            except Exception:
                                logger.exception("Automatic renewal failed; retrying")
                        renewal = asyncio.create_task(reconcile(app))
                except Exception:
                    logger.exception("Automatic reconciliation failed; retrying on next tick")
                await asyncio.sleep(30)
        finally:
            if renewal is not None:
                renewal.cancel()
                try:
                    await renewal
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("Renewal failed during shutdown")

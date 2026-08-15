from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response, StreamingResponse

from ..agent_release import agent_update_available, latest_agent_version, supports_self_update
from ..config import AppConfig
from ..db import Database, dumps, loads_object, merged_settings
from ..deps import get_db, get_event_hub
from ..events import EventHub
from ..jobs import append_log, create_job, finish_job, get_job, job_from_row
from ..live_ops import (
    cleanup_bundle,
    extract_domain_bundle,
    mark_domain_error,
    run_domain_script,
    test_dns_channel_live,
    update_domain_state,
    upload_domain_bundle,
    webdav_probe,
)
from ..schemas import (
    AssignmentUpdate,
    BackupPayload,
    BulkDomainActionRequest,
    DnsChannelCreate,
    DnsChannelPatch,
    DomainCreate,
    DomainPatch,
    NodeCommandRequest,
    NodeCreate,
    NodePatch,
    SettingsPayload,
    TelegramSettings,
    WebDavSettings,
)
from ..security import hash_secret, new_node_token, require_admin
from ..serializers import public_assignment, public_dns_channel, public_domain, public_node
from ..telegram import send_telegram_message
from ..timeutil import iso_now

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/overview")
async def overview(request: Request, db: Database = Depends(get_db), event_hub: EventHub = Depends(get_event_hub)) -> dict[str, Any]:
    domain_rows = db.query_all("SELECT * FROM domains ORDER BY domain")
    node_rows = db.query_all("SELECT * FROM nodes ORDER BY name")
    jobs_failed = db.query_one("SELECT COUNT(*) AS count FROM jobs WHERE status = 'failed'")
    domains = [public_domain(row) for row in domain_rows]
    nodes = [public_node(db, row, request.app.state.config) for row in node_rows]
    return {
        "stats": {
            "onlineNodes": sum(1 for node in nodes if node["isOnline"]),
            "totalNodes": len(nodes),
            "totalDomains": len(domains),
            "expiringSoon": sum(1 for domain in domains if (domain["daysRemaining"] is not None and domain["daysRemaining"] <= 7)),
            "failedJobs": int(jobs_failed["count"]) if jobs_failed else 0,
        },
        "certificates": domains,
        "nodes": nodes,
        "recentEvents": event_hub.recent(20),
    }


@router.get("/events/stream")
async def event_stream(event_hub: EventHub = Depends(get_event_hub)) -> StreamingResponse:
    async def generator():
        async for event in event_hub.stream():
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/domains")
async def list_domains(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.query_all("SELECT * FROM domains ORDER BY domain")
    return [public_domain(row) for row in rows]


@router.post("/domains")
async def create_domain(payload: DomainCreate, db: Database = Depends(get_db)) -> dict[str, Any]:
    channel = db.query_one("SELECT id FROM dns_channels WHERE id = ?", (payload.dnsChannelId,))
    if channel is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "DNS channel not found"})
    now = iso_now()
    domain_id = f"d_{uuid4().hex}"
    try:
        db.execute(
            """
            INSERT INTO domains
                (id, domain, enabled, dns_channel_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (domain_id, payload.domain, int(payload.enabled), payload.dnsChannelId, now, now),
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": str(exc)}) from exc
    return _require_domain(db, domain_id)


@router.patch("/domains/{domain_id}")
async def patch_domain(domain_id: str, payload: DomainPatch, db: Database = Depends(get_db)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM domains WHERE id = ?", (domain_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Domain not found"})
    updates: list[str] = []
    params: list[Any] = []
    if payload.domain is not None:
        updates.append("domain = ?")
        params.append(payload.domain)
    if payload.dnsChannelId is not None:
        channel = db.query_one("SELECT id FROM dns_channels WHERE id = ?", (payload.dnsChannelId,))
        if channel is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "DNS channel not found"})
        updates.append("dns_channel_id = ?")
        params.append(payload.dnsChannelId)
    if payload.enabled is not None:
        updates.append("enabled = ?")
        params.append(int(payload.enabled))
    if updates:
        updates.append("updated_at = ?")
        params.append(iso_now())
        params.append(domain_id)
        db.execute(f"UPDATE domains SET {', '.join(updates)} WHERE id = ?", params)
    return _require_domain(db, domain_id)


@router.delete("/domains/{domain_id}")
async def delete_domain(domain_id: str, db: Database = Depends(get_db)) -> dict[str, bool]:
    db.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
    return {"success": True}


@router.post("/domains/{domain_id}/{action}")
async def run_domain_action(
    domain_id: str,
    action: str,
    request: Request,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, Any]:
    if action not in {"issue", "renew", "sync"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Unknown domain action"})
    row = db.query_one("SELECT * FROM domains WHERE id = ?", (domain_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Domain not found"})
    job = create_job(db, event_hub, action, domain_id, row["domain"])
    append_log(db, job["id"], f"[INFO] Requested {action} for {row['domain']}")
    bundle = None
    try:
        if action == "sync":
            bundle = extract_domain_bundle(request.app.state.config, _settings(db), row["domain"])
            append_log(db, job["id"], f"[INFO] Exported local certificate bundle for {row['domain']}")
            upload_domain_bundle(_settings(db).get("webdav", {}), row["domain"], bundle)
            append_log(db, job["id"], f"[INFO] Uploaded certificate bundle to WebDAV for {row['domain']}")
            update_domain_state(db, domain_id, bundle, mark_synced=True)
        else:
            bundle = await run_domain_script(
                request.app.state.config,
                db,
                domain_id,
                force_reissue=True,
                line_logger=lambda line: append_log(db, job["id"], line),
            )
            update_domain_state(db, domain_id, bundle, mark_issued=True, mark_synced=True)

        append_log(db, job["id"], f"[INFO] Real {action} execution completed for {row['domain']}")
        return finish_job(db, event_hub, job["id"])
    except Exception as exc:
        error_message = str(exc)
        mark_domain_error(db, domain_id, error_message)
        append_log(db, job["id"], f"[ERROR] {error_message}")
        finish_job(db, event_hub, job["id"], status="failed", error=error_message)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": error_message}) from exc
    finally:
        cleanup_bundle(bundle)


@router.post("/domains/bulk-action")
async def run_bulk_domain_action(
    payload: BulkDomainActionRequest,
    request: Request,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, Any]:
    if payload.action not in {"issue", "renew", "sync"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Unsupported bulk domain action"})
    if not payload.ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "No domains selected"})

    unique_ids = list(dict.fromkeys(payload.ids))
    domains_by_id: dict[str, dict[str, Any]] = {}
    for domain_id in unique_ids:
        row = db.query_one("SELECT * FROM domains WHERE id = ?", (domain_id,))
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": f"Domain not found: {domain_id}"})
        domains_by_id[domain_id] = row

    selected_domains = [domains_by_id[domain_id] for domain_id in unique_ids]
    domain_names = [str(row["domain"]) for row in selected_domains]
    target_name = (
        f"{len(domain_names)} domains"
        if len(domain_names) > 3
        else ", ".join(domain_names)
    )
    job = create_job(db, event_hub, payload.action, "bulk", target_name)
    append_log(db, job["id"], f"[INFO] Requested bulk {payload.action} for {len(domain_names)} domains.")
    append_log(db, job["id"], f"[INFO] Domains: {', '.join(domain_names)}")

    successes: list[str] = []
    failures: list[tuple[str, str]] = []
    sync_marks: list[str] = []

    for row in selected_domains:
        domain_id = str(row["id"])
        domain_name = str(row["domain"])
        bundle = None
        append_log(db, job["id"], f"[INFO] ---- Processing {domain_name} ----")
        try:
            if payload.action == "sync":
                bundle = extract_domain_bundle(request.app.state.config, _settings(db), domain_name)
                append_log(db, job["id"], f"[INFO] Exported local certificate bundle for {domain_name}")
                upload_domain_bundle(_settings(db).get("webdav", {}), domain_name, bundle)
                append_log(db, job["id"], f"[INFO] Uploaded certificate bundle to WebDAV for {domain_name}")
                update_domain_state(db, domain_id, bundle, mark_synced=True)
                sync_marks.append(domain_name)
            else:
                bundle = await run_domain_script(
                    request.app.state.config,
                    db,
                    domain_id,
                    force_reissue=True,
                    telegram_enabled=False,
                    line_logger=lambda line, current_domain=domain_name: append_log(
                        db,
                        job["id"],
                        f"[{current_domain}] {line}",
                    ),
                )
                update_domain_state(db, domain_id, bundle, mark_issued=True, mark_synced=True)
            successes.append(domain_name)
            append_log(db, job["id"], f"[INFO] {domain_name} completed successfully.")
        except Exception as exc:
            error_message = str(exc)
            failures.append((domain_name, error_message))
            mark_domain_error(db, domain_id, error_message)
            append_log(db, job["id"], f"[ERROR] [{domain_name}] {error_message}")
        finally:
            cleanup_bundle(bundle)

    append_log(
        db,
        job["id"],
        f"[INFO] Bulk {payload.action} summary: success={len(successes)} failed={len(failures)} total={len(domain_names)}",
    )
    await _send_bulk_domain_summary(
        db,
        job["id"],
        payload.action,
        successes,
        failures,
        sync_marks,
    )

    if failures:
        return finish_job(
            db,
            event_hub,
            job["id"],
            status="failed",
            error=f"{len(failures)} domain(s) failed during bulk {payload.action}",
        )
    return finish_job(db, event_hub, job["id"])


@router.get("/dns-channels")
async def list_dns_channels(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.query_all("SELECT * FROM dns_channels ORDER BY name")
    return [public_dns_channel(row) for row in rows]


@router.post("/dns-channels")
async def create_dns_channel(payload: DnsChannelCreate, db: Database = Depends(get_db)) -> dict[str, Any]:
    now = iso_now()
    channel_id = f"c_{uuid4().hex}"
    db.execute(
        """
        INSERT INTO dns_channels (id, name, provider, credentials_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (channel_id, payload.name, payload.provider, dumps(payload.credentials), now, now),
    )
    return _require_dns_channel(db, channel_id)


@router.patch("/dns-channels/{channel_id}")
async def patch_dns_channel(channel_id: str, payload: DnsChannelPatch, db: Database = Depends(get_db)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM dns_channels WHERE id = ?", (channel_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "DNS channel not found"})
    updates: list[str] = []
    params: list[Any] = []
    if payload.name is not None:
        updates.append("name = ?")
        params.append(payload.name)
    if payload.provider is not None:
        updates.append("provider = ?")
        params.append(payload.provider)
    if payload.credentials is not None:
        current = loads_object(row.get("credentials_json"))
        merged = current if payload.provider is None or payload.provider == row["provider"] else {}
        for key, value in payload.credentials.items():
            if value and value != "***":
                merged[key] = value
        updates.append("credentials_json = ?")
        params.append(dumps(merged))
    if updates:
        updates.append("updated_at = ?")
        params.append(iso_now())
        params.append(channel_id)
        db.execute(f"UPDATE dns_channels SET {', '.join(updates)} WHERE id = ?", params)
    return _require_dns_channel(db, channel_id)


@router.delete("/dns-channels/{channel_id}")
async def delete_dns_channel(channel_id: str, db: Database = Depends(get_db)) -> dict[str, bool]:
    in_use = db.query_one("SELECT COUNT(*) AS count FROM domains WHERE dns_channel_id = ?", (channel_id,))
    if in_use and int(in_use["count"]) > 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "DNS channel is used by domains"})
    db.execute("DELETE FROM dns_channels WHERE id = ?", (channel_id,))
    return {"success": True}


@router.post("/dns-channels/{channel_id}/test")
async def test_dns_channel(
    channel_id: str,
    request: Request,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, bool]:
    channel = db.query_one("SELECT * FROM dns_channels WHERE id = ?", (channel_id,))
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "DNS channel not found"})
    job = create_job(db, event_hub, "test_dns", channel_id, channel["name"])
    try:
        append_log(db, job["id"], f"[INFO] Running live DNS challenge test for channel {channel['name']}")
        await test_dns_channel_live(
            request.app.state.config,
            db,
            channel_id,
            line_logger=lambda line: append_log(db, job["id"], line),
        )
        append_log(db, job["id"], "[INFO] DNS challenge test passed")
        finish_job(db, event_hub, job["id"])
        return {"success": True}
    except ValueError as exc:
        error_message = str(exc)
        append_log(db, job["id"], f"[ERROR] {error_message}")
        finish_job(db, event_hub, job["id"], status="failed", error=error_message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": error_message}) from exc
    except Exception as exc:
        error_message = str(exc)
        append_log(db, job["id"], f"[ERROR] {error_message}")
        finish_job(db, event_hub, job["id"], status="failed", error=error_message)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": error_message}) from exc


@router.get("/nodes")
async def list_nodes(request: Request, db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.query_all("SELECT * FROM nodes ORDER BY name")
    return [public_node(db, row, request.app.state.config) for row in rows]


@router.post("/nodes")
async def create_node(payload: NodeCreate, request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
    now = iso_now()
    node_id = f"n_{uuid4().hex}"
    token = new_node_token()
    node_name = _normalize_node_name(payload.name)
    db.execute(
        """
        INSERT INTO nodes
            (id, name, ip, is_online, cert_dir, token_hash, created_at, updated_at)
        VALUES (?, ?, ?, 0, ?, ?, ?, ?)
        """,
        (node_id, node_name, payload.ip, payload.certDir, hash_secret(token), now, now),
    )
    node = _require_node(db, node_id, request.app.state.config)
    node["token"] = token
    return node


@router.get("/nodes/{node_id}")
async def get_node(node_id: str, request: Request, db: Database = Depends(get_db), event_hub: EventHub = Depends(get_event_hub)) -> dict[str, Any]:
    return _node_detail(db, event_hub, node_id, request.app.state.config)


@router.post("/nodes/{node_id}/rotate-token")
async def rotate_node_token(
    node_id: str,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, str]:
    row = db.query_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Node not found"})
    token = new_node_token()
    now = iso_now()
    db.execute(
        "UPDATE nodes SET token_hash = ?, updated_at = ? WHERE id = ?",
        (hash_secret(token), now, node_id),
    )
    event_hub.publish(
        "node_token_rotated",
        "warning",
        f"Node token rotated for {row['name']}",
        {"nodeId": node_id},
    )
    return {"token": token}


@router.patch("/nodes/{node_id}")
async def patch_node(node_id: str, payload: NodePatch, request: Request, db: Database = Depends(get_db)) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Node not found"})
    updates: list[str] = []
    params: list[Any] = []
    for field, column in (("name", "name"), ("ip", "ip"), ("certDir", "cert_dir"), ("lastError", "last_error")):
        value = getattr(payload, field)
        if value is not None:
            if field == "name":
                value = _normalize_node_name(value)
            updates.append(f"{column} = ?")
            params.append(value)
    if updates:
        updates.append("updated_at = ?")
        params.append(iso_now())
        params.append(node_id)
        db.execute(f"UPDATE nodes SET {', '.join(updates)} WHERE id = ?", params)
    return _require_node(db, node_id, request.app.state.config)


@router.delete("/nodes/{node_id}")
async def delete_node(node_id: str, db: Database = Depends(get_db)) -> dict[str, bool]:
    db.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
    return {"success": True}


@router.put("/nodes/{node_id}/assignments")
async def update_node_assignments(
    node_id: str,
    payload: AssignmentUpdate,
    request: Request,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, Any]:
    node = db.query_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Node not found"})
    now = iso_now()
    db.execute("DELETE FROM node_assignments WHERE node_id = ?", (node_id,))
    for domain_id in payload.domainIds:
        domain = db.query_one("SELECT * FROM domains WHERE id = ?", (domain_id,))
        if domain is None:
            continue
        db.execute(
            """
            INSERT INTO node_assignments
                (id, node_id, domain_id, desired_sha256, deployed_sha256, status, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, NULL, 'pending', ?, ?, ?)
            """,
            (
                f"a_{uuid4().hex}",
                node_id,
                domain_id,
                domain.get("cert_sha256"),
                domain.get("expires_at"),
                now,
                now,
            ),
        )
    event_hub.publish(
        "job_finished",
        "success",
        f"Assignments updated for {node['name']}",
        {"nodeId": node_id, "domainIds": payload.domainIds},
    )
    return _node_detail(db, event_hub, node_id, request.app.state.config)


@router.post("/nodes/{node_id}/run-now")
async def run_node_now(
    node_id: str,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, Any]:
    return _queue_node_command(db, event_hub, node_id, "sync_all", [])


@router.post("/nodes/{node_id}/deploy")
async def deploy_node_domains(
    node_id: str,
    payload: NodeCommandRequest,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, Any]:
    return _queue_node_command(db, event_hub, node_id, "sync_domains", payload.domainIds)


@router.post("/nodes/{node_id}/delete-certs")
async def delete_node_domain_certs(
    node_id: str,
    payload: NodeCommandRequest,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, Any]:
    return _queue_node_command(db, event_hub, node_id, "delete_domains", payload.domainIds)


@router.post("/nodes/{node_id}/upgrade-agent")
async def upgrade_node_agent(
    node_id: str,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, Any]:
    return _queue_node_upgrade(db, event_hub, node_id)


@router.get("/jobs")
async def list_jobs(db: Database = Depends(get_db)) -> list[dict[str, Any]]:
    rows = db.query_all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200")
    return [job_from_row(row) for row in rows]


@router.get("/jobs/{job_id}")
async def get_job_endpoint(job_id: str, db: Database = Depends(get_db)) -> dict[str, Any]:
    try:
        return get_job(db, job_id)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Job not found"}) from exc


@router.get("/jobs/{job_id}/logs")
async def get_job_logs(job_id: str, db: Database = Depends(get_db)) -> dict[str, str]:
    row = db.query_one("SELECT log_text FROM jobs WHERE id = ?", (job_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Job not found"})
    return {"logs": row.get("log_text") or ""}


@router.get("/settings")
async def get_settings(db: Database = Depends(get_db)) -> dict[str, Any]:
    return _settings(db)


@router.patch("/settings")
async def patch_settings(payload: SettingsPayload, db: Database = Depends(get_db)) -> dict[str, Any]:
    now = iso_now()
    db.execute(
        "UPDATE app_settings SET value = ?, updated_at = ? WHERE key = 'settings'",
        (dumps(payload.model_dump()), now),
    )
    return _settings(db)


@router.get("/backup")
async def download_backup(db: Database = Depends(get_db)) -> Response:
    exported_at = iso_now()
    payload = {
        "version": 1,
        "exportedAt": exported_at,
        "settings": _settings(db),
        "dnsChannels": [
            {
                "id": row["id"],
                "name": row["name"],
                "provider": row["provider"],
                "credentials": loads_object(row.get("credentials_json")),
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in db.query_all("SELECT * FROM dns_channels ORDER BY name")
        ],
        "domains": [
            {
                "id": row["id"],
                "domain": row["domain"],
                "enabled": bool(row["enabled"]),
                "dnsChannelId": row["dns_channel_id"],
                "expiresAt": row.get("expires_at"),
                "lastIssuedAt": row.get("last_issued_at"),
                "lastSyncAt": row.get("last_sync_at"),
                "certSha256": row.get("cert_sha256"),
                "status": row.get("status") or "pending",
                "lastError": row.get("last_error"),
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in db.query_all("SELECT * FROM domains ORDER BY domain")
        ],
        "nodes": [
            {
                "id": row["id"],
                "name": row["name"],
                "ip": row.get("ip") or "",
                "isOnline": bool(row.get("is_online")),
                "lastHeartbeatAt": row.get("last_heartbeat_at"),
                "certDir": row.get("cert_dir") or "/etc/nginx/ssl",
                "lastError": row.get("last_error"),
                "tokenHash": row["token_hash"],
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in db.query_all("SELECT * FROM nodes ORDER BY name")
        ],
        "assignments": [
            {
                "id": row["id"],
                "nodeId": row["node_id"],
                "domainId": row["domain_id"],
                "desiredSha256": row.get("desired_sha256"),
                "deployedSha256": row.get("deployed_sha256"),
                "status": row.get("status") or "pending",
                "lastDeployAt": row.get("last_deploy_at"),
                "expiresAt": row.get("expires_at"),
                "lastError": row.get("last_error"),
                "createdAt": row["created_at"],
                "updatedAt": row["updated_at"],
            }
            for row in db.query_all("SELECT * FROM node_assignments ORDER BY node_id, domain_id")
        ],
    }
    filename = f"ssl-sync-backup-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/backup/restore")
async def restore_backup(
    payload: BackupPayload,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, bool]:
    if payload.version != 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": f"Unsupported backup version: {payload.version}"})

    now = iso_now()
    try:
        with db.connect() as conn:
            conn.execute("DELETE FROM jobs")
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM node_assignments")
            conn.execute("DELETE FROM nodes")
            conn.execute("DELETE FROM domains")
            conn.execute("DELETE FROM dns_channels")
            conn.execute(
                "UPDATE app_settings SET value = ?, updated_at = ? WHERE key = 'settings'",
                (dumps(payload.settings.model_dump()), now),
            )
            conn.executemany(
                """
                INSERT INTO dns_channels (id, name, provider, credentials_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.id,
                        item.name,
                        item.provider,
                        dumps(item.credentials),
                        item.createdAt,
                        item.updatedAt,
                    )
                    for item in payload.dnsChannels
                ],
            )
            conn.executemany(
                """
                INSERT INTO domains
                    (id, domain, enabled, dns_channel_id, expires_at, last_issued_at, last_sync_at, cert_sha256, status, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.id,
                        item.domain,
                        int(item.enabled),
                        item.dnsChannelId,
                        item.expiresAt,
                        item.lastIssuedAt,
                        item.lastSyncAt,
                        item.certSha256,
                        item.status,
                        item.lastError,
                        item.createdAt,
                        item.updatedAt,
                    )
                    for item in payload.domains
                ],
            )
            conn.executemany(
                """
                INSERT INTO nodes
                    (id, name, ip, is_online, last_heartbeat_at, cert_dir, last_error, token_hash, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.id,
                        item.name,
                        item.ip,
                        int(item.isOnline),
                        item.lastHeartbeatAt,
                        item.certDir,
                        item.lastError,
                        item.tokenHash,
                        item.createdAt,
                        item.updatedAt,
                    )
                    for item in payload.nodes
                ],
            )
            conn.executemany(
                """
                INSERT INTO node_assignments
                    (id, node_id, domain_id, desired_sha256, deployed_sha256, status, last_deploy_at, expires_at, last_error, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.id,
                        item.nodeId,
                        item.domainId,
                        item.desiredSha256,
                        item.deployedSha256,
                        item.status,
                        item.lastDeployAt,
                        item.expiresAt,
                        item.lastError,
                        item.createdAt,
                        item.updatedAt,
                    )
                    for item in payload.assignments
                ],
            )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": f"Failed to restore backup: {exc}"}) from exc

    event_hub.publish(
        "job_finished",
        "warning",
        "Configuration restored from backup",
        {
            "dnsChannels": len(payload.dnsChannels),
            "domains": len(payload.domains),
            "nodes": len(payload.nodes),
            "assignments": len(payload.assignments),
        },
    )
    return {"success": True}


@router.post("/settings/webdav/test")
async def test_webdav(
    payload: WebDavSettings | None = None,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, bool]:
    settings = payload.model_dump() if payload is not None else _settings(db).get("webdav", {})
    job = create_job(db, event_hub, "sync", "settings", "WebDAV")
    url = settings.get("url") or "(empty)"
    append_log(db, job["id"], f"[INFO] WebDAV URL configured as: {url}")
    try:
        await asyncio.to_thread(webdav_probe, settings)
        append_log(db, job["id"], "[INFO] Live WebDAV probe completed successfully")
        finish_job(db, event_hub, job["id"])
        return {"success": True}
    except ValueError as exc:
        error_message = str(exc)
        append_log(db, job["id"], f"[ERROR] {error_message}")
        finish_job(db, event_hub, job["id"], status="failed", error=error_message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": error_message}) from exc
    except Exception as exc:
        error_message = str(exc)
        append_log(db, job["id"], f"[ERROR] {error_message}")
        finish_job(db, event_hub, job["id"], status="failed", error=error_message)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": error_message}) from exc


@router.post("/settings/telegram/test")
async def test_telegram(
    payload: TelegramSettings | None = None,
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, bool]:
    telegram_settings = payload.model_dump() if payload is not None else _settings(db).get("telegram", {})
    job = create_job(db, event_hub, "sync", "settings", "Telegram")
    bot_token = str(telegram_settings.get("botToken") or "").strip()
    chat_id = str(telegram_settings.get("chatId") or "").strip()

    if not bot_token or not chat_id:
        error_message = "Telegram Bot Token or Chat ID is empty"
        append_log(db, job["id"], f"[ERROR] {error_message}")
        finish_job(db, event_hub, job["id"], status="failed", error=error_message)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": error_message})

    append_log(db, job["id"], f"[INFO] Sending test message to chat {chat_id}")

    try:
        response = await asyncio.to_thread(
            send_telegram_message,
            bot_token,
            chat_id,
            "SSL Sync Master 测试消息：Telegram 推送已连接。",
        )
    except Exception as exc:
        error_message = str(exc)
        append_log(db, job["id"], f"[ERROR] {error_message}")
        finish_job(db, event_hub, job["id"], status="failed", error=error_message)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"error": error_message}) from exc

    message_id = (
        response.get("result", {}).get("message_id")
        if isinstance(response.get("result"), dict)
        else None
    )
    append_log(db, job["id"], f"[INFO] Telegram message sent successfully. message_id={message_id or '(unknown)'}")
    finish_job(db, event_hub, job["id"])
    return {"success": True}


async def _send_bulk_domain_summary(
    db: Database,
    job_id: str,
    action: str,
    successes: list[str],
    failures: list[tuple[str, str]],
    _sync_marks: list[str],
) -> None:
    settings = _settings(db)
    telegram_settings = settings.get("telegram", {})
    bot_token = str(telegram_settings.get("botToken") or "").strip()
    chat_id = str(telegram_settings.get("chatId") or "").strip()
    if not bot_token or not chat_id:
        append_log(db, job_id, "[INFO] Telegram is not configured, skipped bulk summary notification.")
        return

    action_label = {
        "issue": "批量申请",
        "renew": "批量续签",
        "sync": "批量同步",
    }.get(action, action)
    icon = "⚠️" if failures else "✅"
    title = f"{icon} [BULK] {action_label}完成"

    lines = [
        f"动作: {action_label}",
        f"成功: {len(successes)}",
        f"失败: {len(failures)}",
    ]
    if successes:
        success_label = "已同步" if action == "sync" else "已完成"
        lines.append("")
        lines.append(f"{success_label}:")
        lines.extend(f"• {domain}" for domain in successes)
    if failures:
        lines.append("")
        lines.append("失败:")
        lines.extend(f"• {domain}: {message}" for domain, message in failures)
    try:
        response = await asyncio.to_thread(send_telegram_message, bot_token, chat_id, f"{title}\n\n" + "\n".join(lines))
    except Exception as exc:
        append_log(db, job_id, f"[WARN] Failed to send bulk Telegram summary: {exc}")
        return

    message_id = (
        response.get("result", {}).get("message_id")
        if isinstance(response.get("result"), dict)
        else None
    )
    append_log(db, job_id, f"[INFO] Bulk Telegram summary sent successfully. message_id={message_id or '(unknown)'}")


def _settings(db: Database) -> dict[str, Any]:
    row = db.query_one("SELECT value FROM app_settings WHERE key = 'settings'")
    return merged_settings(row["value"] if row else "{}")


def _queue_node_command(
    db: Database,
    event_hub: EventHub,
    node_id: str,
    command_type: str,
    requested_domain_ids: list[str],
) -> dict[str, Any]:
    node_row = db.query_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if node_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Node not found"})

    assignment_rows = db.query_all(
        """
        SELECT a.domain_id, d.domain
        FROM node_assignments a
        JOIN domains d ON d.id = a.domain_id
        WHERE a.node_id = ?
        ORDER BY d.domain
        """,
        (node_id,),
    )
    assignments_by_id = {row["domain_id"]: row for row in assignment_rows}

    if command_type == "sync_all":
        domain_ids = list(assignments_by_id.keys())
    else:
        domain_ids = [domain_id for domain_id in requested_domain_ids if domain_id in assignments_by_id]

    if not domain_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "No assigned domains selected for this node"})

    domain_names = [str(assignments_by_id[domain_id]["domain"]) for domain_id in domain_ids]
    now = iso_now()

    if command_type == "delete_domains":
        job_type = "delete"
        summary = "delete local certificates"
    else:
        job_type = "deploy"
        summary = "deploy certificates"

    target_name = node_row["name"] if command_type == "sync_all" else f"{node_row['name']} ({len(domain_names)} domain{'s' if len(domain_names) != 1 else ''})"
    job = create_job(db, event_hub, job_type, node_id, target_name)
    append_log(db, job["id"], f"[INFO] Requested to {summary} on node {node_row['name']}.")
    append_log(db, job["id"], f"[INFO] Domains: {', '.join(domain_names)}")
    append_log(db, job["id"], "[INFO] Command queued for the node agent poller.")

    db.execute(
        """
        INSERT INTO node_commands
            (id, node_id, job_id, type, payload_json, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            f"cmd_{uuid4().hex}",
            node_id,
            job["id"],
            command_type,
            dumps(
                {
                    "jobId": job["id"],
                    "requestedAt": now,
                    "source": "web",
                    "domainIds": domain_ids,
                    "domainNames": domain_names,
                }
            ),
            now,
            now,
        ),
    )

    event_hub.publish(
        "job_started",
        "info",
        f"Node command queued for {node_row['name']}",
        {"nodeId": node_id, "jobId": job["id"], "commandType": command_type, "domainIds": domain_ids},
    )
    return get_job(db, job["id"])


def _queue_node_upgrade(db: Database, event_hub: EventHub, node_id: str) -> dict[str, Any]:
    node_row = db.query_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if node_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Node not found"})

    current_version = str(node_row.get("agent_version") or "")
    target_version = latest_agent_version()
    if not current_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "Node agent version is unknown. Wait for a heartbeat or bootstrap the agent manually."},
        )
    if not agent_update_available(current_version):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Node agent is already up to date"})
    if not supports_self_update(current_version):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "This node agent is too old for one-click upgrades and requires one manual bootstrap update."},
        )

    pending = db.query_one(
        "SELECT id FROM node_commands WHERE node_id = ? AND type = 'upgrade_agent' AND status = 'pending' LIMIT 1",
        (node_id,),
    )
    if pending is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"error": "An agent upgrade is already queued"})

    now = iso_now()
    job = create_job(db, event_hub, "upgrade", node_id, node_row["name"])
    append_log(db, job["id"], f"[INFO] Requested node agent upgrade: {current_version} -> {target_version}")
    append_log(db, job["id"], "[INFO] Upgrade command queued for the node agent poller.")
    command_id = f"cmd_{uuid4().hex}"
    db.execute(
        """
        INSERT INTO node_commands
            (id, node_id, job_id, type, payload_json, status, created_at, updated_at)
        VALUES (?, ?, ?, 'upgrade_agent', ?, 'pending', ?, ?)
        """,
        (
            command_id,
            node_id,
            job["id"],
            dumps({"jobId": job["id"], "requestedAt": now, "source": "web", "targetVersion": target_version}),
            now,
            now,
        ),
    )
    event_hub.publish(
        "job_started",
        "info",
        f"Node agent upgrade queued for {node_row['name']}",
        {"nodeId": node_id, "jobId": job["id"], "commandId": command_id, "targetVersion": target_version},
    )
    return get_job(db, job["id"])


def _require_domain(db: Database, domain_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM domains WHERE id = ?", (domain_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Domain not found"})
    return public_domain(row)


def _normalize_node_name(value: str) -> str:
    name = str(value).strip()
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Node name cannot be empty"})
    return name


def _require_dns_channel(db: Database, channel_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM dns_channels WHERE id = ?", (channel_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "DNS channel not found"})
    return public_dns_channel(row)


def _require_node(db: Database, node_id: str, config: AppConfig) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Node not found"})
    return public_node(db, row, config)


def _node_detail(db: Database, event_hub: EventHub, node_id: str, config: AppConfig) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Node not found"})
    assignment_rows = db.query_all(
        """
        SELECT a.*, d.domain
        FROM node_assignments a
        JOIN domains d ON d.id = a.domain_id
        WHERE a.node_id = ?
        ORDER BY d.domain
        """,
        (node_id,),
    )
    node = public_node(db, row, config)
    node["assignments"] = [public_assignment(item) for item in assignment_rows]
    node["recentEvents"] = [
        event for event in event_hub.recent(50)
        if event.get("payload", {}).get("nodeId") == node_id
    ][:10]
    return node

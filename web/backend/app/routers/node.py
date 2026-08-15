from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import tarfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse, Response

from ..agent_release import build_agent_bundle, latest_agent_version, node_asset_dir
from ..db import Database, loads_object, merged_settings
from ..deps import get_db, get_event_hub
from ..events import EventHub
from ..jobs import append_log, finish_job
from ..live_ops import cleanup_bundle, extract_domain_bundle
from ..schemas import NodeCommandAck, NodeHeartbeat, NodeReport
from ..security import require_node
from ..serializers import public_assignment
from ..telegram import send_telegram_message
from ..timeutil import iso_now

router = APIRouter(prefix="/api/node/v1", tags=["node"])
bootstrap_router = APIRouter(tags=["node"])
logger = logging.getLogger("ssl_sync.node")


@bootstrap_router.get("/api/agent.sh", include_in_schema=False)
async def download_agent_script(request: Request, db: Database = Depends(get_db)) -> PlainTextResponse:
    master_url = _resolve_public_base_url(request, db)
    asset_dir = _node_asset_dir()
    agent_script = (asset_dir / "cert-node-agent.sh").read_text(encoding="utf-8")
    pull_script = (asset_dir / "cert-node-pull.sh").read_text(encoding="utf-8")
    service_unit = (asset_dir / "cert-puller.service").read_text(encoding="utf-8")
    timer_unit = (asset_dir / "cert-puller.timer").read_text(encoding="utf-8")
    content = _render_agent_installer(master_url, agent_script, pull_script, service_unit, timer_unit)
    return PlainTextResponse(content, media_type="text/x-shellscript; charset=utf-8")


@router.post("/heartbeat")
async def heartbeat(
    payload: NodeHeartbeat,
    request: Request,
    node: dict[str, Any] = Depends(require_node),
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, Any]:
    reported_ip = _normalize_ip(payload.ip)
    observed_ip = _observed_client_ip(request)
    ip = reported_ip or observed_ip or node.get("ip") or ""
    cert_dir = payload.certDir or node.get("cert_dir") or "/etc/nginx/ssl"
    now = iso_now()
    db.execute(
        """
        UPDATE nodes
        SET ip = ?, cert_dir = ?, is_online = 1, last_heartbeat_at = ?,
            agent_version = COALESCE(NULLIF(?, ''), agent_version), updated_at = ?
        WHERE id = ?
        """,
        (ip, cert_dir, now, payload.version or "", now, node["id"]),
    )
    event_hub.publish(
        "node_heartbeat",
        "info",
        f"Heartbeat received from {node['name']}",
        {"nodeId": node["id"], "ip": ip, "version": payload.version},
    )
    logger.info(
        "node heartbeat node_id=%s node=%s ip=%s source=%s version=%s",
        node["id"],
        node["name"],
        ip or "unknown",
        "reported" if reported_ip else "observed",
        payload.version or "unknown",
    )
    return {"success": True, "serverTime": now}


@router.get("/agent/bundle")
async def agent_bundle(_node: dict[str, Any] = Depends(require_node)) -> Response:
    version = latest_agent_version()
    return Response(
        content=build_agent_bundle(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="cert-node-agent-{version}.tar.gz"'},
    )


@router.get("/assignments")
async def assignments(node: dict[str, Any] = Depends(require_node), db: Database = Depends(get_db)) -> dict[str, Any]:
    rows = db.query_all(
        """
        SELECT a.*, d.domain
        FROM node_assignments a
        JOIN domains d ON d.id = a.domain_id
        WHERE a.node_id = ?
        ORDER BY d.domain
        """,
        (node["id"],),
    )
    settings_row = db.query_one("SELECT value, updated_at FROM app_settings WHERE key = 'settings'")
    settings = merged_settings(settings_row["value"] if settings_row else "{}")
    webdav = settings.get("webdav", {})
    return {
        "nodeId": node["id"],
        "assignments": [public_assignment(row) for row in rows],
        "settingsUpdatedAt": settings_row.get("updated_at") if settings_row else None,
        "webdav": {
            "url": str(webdav.get("url") or ""),
            "auth": str(webdav.get("auth") or ""),
        },
    }


@router.get("/commands")
async def commands(node: dict[str, Any] = Depends(require_node), db: Database = Depends(get_db)) -> dict[str, Any]:
    rows = db.query_all(
        """
        SELECT * FROM node_commands
        WHERE node_id = ? AND status = 'pending'
        ORDER BY created_at ASC
        """,
        (node["id"],),
    )
    return {
        "nodeId": node["id"],
        "commands": [
            {
                "id": row["id"],
                "type": row["type"],
                "payload": loads_object(row.get("payload_json")),
                "createdAt": row["created_at"],
            }
            for row in rows
        ],
    }


@router.get("/certificates/{domain}/sha256")
async def certificate_sha256(
    domain: str,
    node: dict[str, Any] = Depends(require_node),
    db: Database = Depends(get_db),
) -> PlainTextResponse:
    assigned_domain = _assigned_domain_for_node(db, node["id"], domain)
    sha256 = str(assigned_domain.get("cert_sha256") or "").strip()
    if not sha256:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Certificate SHA256 is not available"})
    return PlainTextResponse(f"{sha256}\n", media_type="text/plain; charset=utf-8")


@router.get("/certificates/{domain}/bundle")
async def certificate_bundle(
    domain: str,
    request: Request,
    node: dict[str, Any] = Depends(require_node),
    db: Database = Depends(get_db),
) -> Response:
    assigned_domain = _assigned_domain_for_node(db, node["id"], domain)
    settings_row = db.query_one("SELECT value FROM app_settings WHERE key = 'settings'")
    settings = merged_settings(settings_row["value"] if settings_row else "{}")
    config = request.app.state.config

    bundle = None
    try:
        bundle = extract_domain_bundle(config, settings, assigned_domain["domain"])
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            _add_bytes_to_archive(archive, "fullchain.pem", bundle.chain_file.read_bytes())
            _add_bytes_to_archive(archive, "privkey.pem", bundle.key_file.read_bytes())
            _add_bytes_to_archive(archive, "cert.sha256", f"{bundle.sha256}\n".encode("utf-8"))
        payload = buffer.getvalue()
    finally:
        cleanup_bundle(bundle)

    return Response(
        content=payload,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{domain}.tar.gz"'},
    )


@router.post("/reports")
async def report(
    payload: NodeReport,
    node: dict[str, Any] = Depends(require_node),
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, bool]:
    now = iso_now()
    failed = 0
    first_error: str | None = None
    for item in payload.items:
        if item.status == "error":
            failed += 1
            if first_error is None and item.lastError:
                first_error = item.lastError
        db.execute(
            """
            UPDATE node_assignments
            SET deployed_sha256 = ?,
                status = ?,
                expires_at = ?,
                last_error = ?,
                last_deploy_at = CASE
                    WHEN COALESCE(deployed_sha256, '') <> COALESCE(?, '')
                      OR COALESCE(status, '') <> COALESCE(?, '')
                      OR COALESCE(last_error, '') <> COALESCE(?, '')
                    THEN ?
                    ELSE last_deploy_at
                END,
                updated_at = ?
            WHERE node_id = ? AND domain_id = ?
            """,
            (
                item.deployedSha256,
                item.status,
                item.expiresAt,
                item.lastError,
                item.deployedSha256,
                item.status,
                item.lastError,
                now,
                now,
                node["id"],
                item.domainId,
            ),
        )

    db.execute(
        "UPDATE nodes SET last_error = ?, updated_at = ? WHERE id = ?",
        (first_error if failed else None, now, node["id"]),
    )

    if failed:
        event_hub.publish(
            "deploy_failed",
            "error",
            f"{failed} certificate deployment(s) failed on {node['name']}",
            {"nodeId": node["id"], "failed": failed},
        )
    else:
        event_hub.publish(
            "deploy_success",
            "success",
            f"Deployment report received from {node['name']}",
            {"nodeId": node["id"], "count": len(payload.items)},
        )
    return {"success": True}


@router.post("/commands/{command_id}/ack")
async def ack_command(
    command_id: str,
    payload: NodeCommandAck,
    node: dict[str, Any] = Depends(require_node),
    db: Database = Depends(get_db),
    event_hub: EventHub = Depends(get_event_hub),
) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM node_commands WHERE id = ? AND node_id = ?", (command_id, node["id"]))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Command not found"})

    if row.get("status") != "pending":
        return {"success": True, "nodeId": node["id"], "commandId": command_id}

    now = iso_now()
    command_status = "failed" if payload.status == "failed" else "completed"
    db.execute(
        """
        UPDATE node_commands
        SET status = ?, acked_at = ?, completed_at = ?, last_error = ?, updated_at = ?
        WHERE id = ?
        """,
        (command_status, now, now, payload.error, now, command_id),
    )

    if row.get("type") == "upgrade_agent" and payload.status != "failed":
        command_payload = loads_object(row.get("payload_json"))
        target_version = str(command_payload.get("targetVersion") or "").strip()
        if target_version:
            db.execute(
                "UPDATE nodes SET agent_version = ?, last_error = NULL, updated_at = ? WHERE id = ?",
                (target_version, now, node["id"]),
            )

    if row.get("job_id"):
        job = db.query_one("SELECT status FROM jobs WHERE id = ?", (row["job_id"],))
        if job and job.get("status") == "running":
            append_log(db, row["job_id"], f"[INFO] Node {node['name']} acknowledged command {command_id} ({row['type']}).")
            if payload.summary:
                for line in payload.summary.splitlines():
                    append_log(db, row["job_id"], f"[INFO] {line}")
            if payload.error:
                append_log(db, row["job_id"], f"[ERROR] {payload.error}")
            finish_job(
                db,
                event_hub,
                row["job_id"],
                status="failed" if payload.status == "failed" else "success",
                error=payload.error,
            )
            await _notify_node_command_result(db, event_hub, row, node, payload)

    return {"success": True, "nodeId": node["id"], "commandId": command_id}


def _node_asset_dir() -> Path:
    return node_asset_dir()


def _normalize_ip(value: str | None) -> str:
    candidate = str(value or "").strip().strip('"')
    if not candidate or candidate.lower() == "unknown":
        return ""
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1:candidate.index("]")]
    elif candidate.count(":") == 1 and "." in candidate:
        host, port = candidate.rsplit(":", 1)
        if port.isdigit():
            candidate = host
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return ""


def _observed_client_ip(request: Request) -> str:
    forwarded = request.headers.get("forwarded", "")
    if forwarded:
        for part in forwarded.split(",", 1)[0].split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key.lower() == "for":
                normalized = _normalize_ip(value)
                if normalized:
                    return normalized

    for header_name in ("x-forwarded-for", "x-real-ip"):
        raw_value = request.headers.get(header_name, "")
        if raw_value:
            normalized = _normalize_ip(raw_value.split(",", 1)[0])
            if normalized:
                return normalized

    return _normalize_ip(request.client.host if request.client else "")


def _resolve_public_base_url(request: Request, db: Database) -> str:
    settings_row = db.query_one("SELECT value FROM app_settings WHERE key = 'settings'")
    settings = merged_settings(settings_row["value"] if settings_row else "{}")
    configured = str(settings.get("node", {}).get("publicBaseUrl") or "").strip().rstrip("/")
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def _render_agent_installer(
    master_url: str,
    agent_script: str,
    pull_script: str,
    service_unit: str,
    timer_unit: str,
) -> str:
    master_url_literal = _shell_quote(master_url)
    return f"""#!/usr/bin/env bash
set -euo pipefail

TOKEN=""
MASTER_URL={master_url_literal}
CERT_DIR="/etc/ssl/certs/acme"
CONFIG_FILE="/etc/default/cert-node"
PRESERVE_CONFIG=0

usage() {{
    echo "Usage: bash -s -- --token <node-token> [--master-url <url>] [--cert-dir <path>] [--preserve-config]" >&2
}}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --token)
            TOKEN="${{2:-}}"
            shift 2
            ;;
        --master-url)
            MASTER_URL="${{2:-}}"
            shift 2
            ;;
        --cert-dir)
            CERT_DIR="${{2:-}}"
            shift 2
            ;;
        --preserve-config)
            PRESERVE_CONFIG=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            exit 1
            ;;
    esac
done

if [[ ${{EUID}} -ne 0 ]]; then
    echo "Please run this installer as root." >&2
    exit 1
fi

if [[ -z "${{TOKEN}}" ]]; then
    echo "Missing required --token argument." >&2
    usage
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl openssl python3 tar

mkdir -p /usr/local/bin /etc/default /etc/systemd/system /var/log
install -d -m 700 "${{CERT_DIR}}"

cat > /usr/local/bin/cert-node-agent.sh <<'EOF_SSL_SYNC_NODE_AGENT'
{agent_script.rstrip()}
EOF_SSL_SYNC_NODE_AGENT
chmod 750 /usr/local/bin/cert-node-agent.sh

cat > /usr/local/bin/cert-node-pull.sh <<'EOF_SSL_SYNC_NODE_PULL'
{pull_script.rstrip()}
EOF_SSL_SYNC_NODE_PULL
chmod 750 /usr/local/bin/cert-node-pull.sh

cat > /etc/systemd/system/cert-puller.service <<'EOF_SSL_SYNC_NODE_SERVICE'
{service_unit.rstrip()}
EOF_SSL_SYNC_NODE_SERVICE

cat > /etc/systemd/system/cert-puller.timer <<'EOF_SSL_SYNC_NODE_TIMER'
{timer_unit.rstrip()}
EOF_SSL_SYNC_NODE_TIMER

if [[ -f "${{CONFIG_FILE}}" ]]; then
    cp "${{CONFIG_FILE}}" "${{CONFIG_FILE}}.bak.$(date '+%Y%m%d_%H%M%S')"
fi

if [[ "${{PRESERVE_CONFIG}}" != "1" || ! -f "${{CONFIG_FILE}}" ]]; then
cat > "${{CONFIG_FILE}}" <<EOF_SSL_SYNC_NODE_CONFIG
MASTER_URL='${{MASTER_URL}}'
NODE_TOKEN='${{TOKEN}}'
CERT_BASE_DIR='${{CERT_DIR}}'
TMP_BASE='/tmp/ssl_update'
TG_BOT_TOKEN=''
TG_CHAT_ID=''
SERVICE_TEST_CMD='nginx -t'
SERVICE_RELOAD_CMD='systemctl reload nginx'
LOG_FILE='/var/log/cert-node-pull.log'
NODE_NAME='$(hostname -s)'
NODE_IP=''
NODE_IP_DETECT_URL='https://api.ipify.org'
DOMAINS=()
WEBDAV_URL=''
WEBDAV_AUTH=''
EOF_SSL_SYNC_NODE_CONFIG
chmod 600 "${{CONFIG_FILE}}"
else
    echo "[INFO] Existing node config preserved: ${{CONFIG_FILE}}"
fi

install -m 640 /dev/null /var/log/cert-node-pull.log
systemctl daemon-reload
systemctl enable --now cert-puller.timer

echo "[INFO] Node agent installed successfully."
echo "[INFO] Config file: ${{CONFIG_FILE}}"
echo "[INFO] Trigger a manual sync with: systemctl start cert-puller.service"
echo "[INFO] Follow logs with: journalctl -u cert-puller -f"
"""


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _assigned_domain_for_node(db: Database, node_id: str, domain: str) -> dict[str, Any]:
    row = db.query_one(
        """
        SELECT d.*
        FROM domains d
        JOIN node_assignments a ON a.domain_id = d.id
        WHERE a.node_id = ? AND d.domain = ?
        """,
        (node_id, domain),
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail={"error": "Domain is not assigned to this node"})
    return row


def _add_bytes_to_archive(archive: tarfile.TarFile, name: str, payload: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mode = 0o600
    info.mtime = int(time.time())
    archive.addfile(info, io.BytesIO(payload))


async def _notify_node_command_result(
    db: Database,
    event_hub: EventHub,
    command_row: dict[str, Any],
    node: dict[str, Any],
    payload: NodeCommandAck,
) -> None:
    settings_row = db.query_one("SELECT value FROM app_settings WHERE key = 'settings'")
    settings = merged_settings(settings_row["value"] if settings_row else "{}")
    telegram_settings = settings.get("telegram", {})
    bot_token = str(telegram_settings.get("botToken") or "").strip()
    chat_id = str(telegram_settings.get("chatId") or "").strip()
    if not bot_token or not chat_id:
        return

    payload_json = loads_object(command_row.get("payload_json"))
    domain_names = payload_json.get("domainNames") or []
    if isinstance(domain_names, list):
        domain_list = ", ".join(str(item) for item in domain_names if item)
    else:
        domain_list = ""

    if command_row.get("type") == "delete_domains":
        action_label = "节点证书删除"
    elif command_row.get("type") == "upgrade_agent":
        action_label = "节点 Agent 升级"
    else:
        action_label = "节点证书下发"
    success = payload.status != "failed"
    title = f"{'✅' if success else '🚨'} [NODE] {node['name']} {action_label}{'完成' if success else '失败'}"
    body_parts: list[str] = []
    if domain_list:
        body_parts.append(f"域名: {domain_list}")
    if payload.summary:
        body_parts.append(payload.summary)
    elif payload.error:
        body_parts.append(payload.error)
    text = title if not body_parts else f"{title}\n\n" + "\n".join(body_parts)

    try:
        response = await asyncio.to_thread(send_telegram_message, bot_token, chat_id, text)
    except Exception as exc:
        if command_row.get("job_id"):
            append_log(db, command_row["job_id"], f"[WARN] Failed to send Telegram notification from Master: {exc}")
        event_hub.publish(
            "node_notify_failed",
            "warning",
            f"Failed to send Telegram notification for node {node['name']}",
            {"nodeId": node["id"], "commandId": command_row["id"], "error": str(exc)},
        )
        return

    message_id = (
        response.get("result", {}).get("message_id")
        if isinstance(response.get("result"), dict)
        else None
    )
    if command_row.get("job_id"):
        append_log(
            db,
            command_row["job_id"],
            f"[INFO] Master Telegram notification sent successfully. message_id={message_id or '(unknown)'}",
        )

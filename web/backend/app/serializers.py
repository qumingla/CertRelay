from __future__ import annotations

from typing import Any

from .agent_release import agent_update_available, latest_agent_version, supports_self_update
from .config import AppConfig
from .db import Database, loads_object
from .node_presence import node_presence
from .timeutil import days_remaining


def public_domain(row: dict[str, Any]) -> dict[str, Any]:
    remaining = days_remaining(row.get("expires_at"))
    status = row.get("status") or "pending"
    if status in {"active", "expiring", "expired", "pending"} and row.get("expires_at"):
        if remaining is not None and remaining < 0:
            status = "expired"
        elif remaining is not None and remaining <= 7:
            status = "expiring"
        elif status != "pending":
            status = "active"
    return {
        "id": row["id"],
        "domain": row["domain"],
        "enabled": bool(row["enabled"]),
        "dnsChannelId": row["dns_channel_id"],
        "expiresAt": row.get("expires_at"),
        "daysRemaining": remaining,
        "lastIssuedAt": row.get("last_issued_at"),
        "lastSyncAt": row.get("last_sync_at"),
        "certSha256": row.get("cert_sha256"),
        "status": status,
    }


def mask_credentials(credentials: dict[str, Any]) -> dict[str, str]:
    return {key: "***" if value else "" for key, value in credentials.items()}


def public_dns_channel(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "credentials": mask_credentials(loads_object(row.get("credentials_json"))),
        "createdAt": row["created_at"],
    }


def public_node(
    db: Database,
    row: dict[str, Any],
    config: AppConfig,
) -> dict[str, Any]:
    count_row = db.query_one("SELECT COUNT(*) AS count FROM node_assignments WHERE node_id = ?", (row["id"],))
    presence = node_presence(
        row,
        heartbeat_interval_seconds=config.node_heartbeat_interval_seconds,
        missed_heartbeats=config.node_offline_missed_heartbeats,
    )
    agent_version = str(row.get("agent_version") or "")
    latest_version = latest_agent_version()
    return {
        "id": row["id"],
        "name": row["name"],
        "ip": row.get("ip") or "",
        "isOnline": presence["isOnline"],
        "offlineAt": presence["offlineAt"],
        "lastHeartbeatAt": row.get("last_heartbeat_at"),
        "agentVersion": agent_version or None,
        "latestAgentVersion": latest_version,
        "updateAvailable": bool(agent_version and agent_update_available(agent_version)),
        "supportsSelfUpdate": supports_self_update(agent_version),
        "certDir": row.get("cert_dir") or "/etc/nginx/ssl",
        "assignedDomainsCount": int(count_row["count"]) if count_row else 0,
        "lastError": row.get("last_error"),
    }


def public_assignment(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "nodeId": row["node_id"],
        "domainId": row["domain_id"],
        "domainName": row.get("domain"),
        "desiredSha256": row.get("desired_sha256"),
        "deployedSha256": row.get("deployed_sha256"),
        "status": row.get("status") or "pending",
        "lastDeployAt": row.get("last_deploy_at"),
        "expiresAt": row.get("expires_at"),
        "lastError": row.get("last_error"),
    }

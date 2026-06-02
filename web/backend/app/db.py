from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .security import hash_password
from .timeutil import iso_now


DEFAULT_SETTINGS = {
    "webdav": {
        "url": "",
        "auth": "",
    },
    "telegram": {
        "botToken": "",
        "chatId": "",
    },
    "acme": {
        "acmeHome": "/root/.acme.sh",
        "stagingBase": "/tmp/acme_staging",
        "defaultRenewDays": 7,
        "defaultCa": "letsencrypt",
        "accountEmail": "",
    },
    "node": {
        "publicBaseUrl": "",
    },
}

DEFAULT_AUTH_STATE = {
    "initialized": False,
    "username": "",
    "passwordHash": "",
    "createdAt": None,
    "updatedAt": None,
}

PLACEHOLDER_ADMIN_PASSWORDS = {"", "admin", "change-this", "change-me-before-production"}


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init(self, admin_username: str = "admin", admin_password: str = "admin") -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            existing = conn.execute("SELECT value FROM app_settings WHERE key = 'settings'").fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                    ("settings", json.dumps(DEFAULT_SETTINGS), iso_now()),
                )
            else:
                merged = _merge_defaults(DEFAULT_SETTINGS, loads_object(existing["value"]))
                serialized = dumps(merged)
                if serialized != existing["value"]:
                    conn.execute(
                        "UPDATE app_settings SET value = ?, updated_at = ? WHERE key = 'settings'",
                        (serialized, iso_now()),
                    )

            existing_auth = conn.execute("SELECT value FROM app_settings WHERE key = 'auth'").fetchone()
            if existing_auth is None:
                auth_value = _initial_auth_state(conn, admin_username, admin_password)
                conn.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)",
                    ("auth", dumps(auth_value), iso_now()),
                )
            else:
                merged_auth = merged_auth_state(existing_auth["value"])
                serialized_auth = dumps(merged_auth)
                if serialized_auth != existing_auth["value"]:
                    conn.execute(
                        "UPDATE app_settings SET value = ?, updated_at = ? WHERE key = 'auth'",
                        (serialized_auth, iso_now()),
                    )

    def query_all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row is not None else None

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, tuple(params))

    def execute_many(self, sql: str, params: Iterable[Iterable[Any]]) -> None:
        with self.connect() as conn:
            conn.executemany(sql, [tuple(item) for item in params])


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def merged_settings(value: str | None) -> dict[str, Any]:
    return _merge_defaults(DEFAULT_SETTINGS, loads_object(value))


def merged_auth_state(value: str | None) -> dict[str, Any]:
    return _merge_defaults(DEFAULT_AUTH_STATE, loads_object(value))


def _initial_auth_state(conn: sqlite3.Connection, admin_username: str, admin_password: str) -> dict[str, Any]:
    if _has_existing_runtime_data(conn) or _should_seed_admin_from_config(admin_password):
        return _build_auth_state(admin_username, admin_password)
    return dict(DEFAULT_AUTH_STATE)


def _has_existing_runtime_data(conn: sqlite3.Connection) -> bool:
    for table in ("dns_channels", "domains", "nodes", "jobs", "events"):
        row = conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        if row is not None:
            return True
    return False


def _should_seed_admin_from_config(admin_password: str) -> bool:
    return admin_password.strip() not in PLACEHOLDER_ADMIN_PASSWORDS


def _build_auth_state(username: str, password: str) -> dict[str, Any]:
    now = iso_now()
    return {
        "initialized": True,
        "username": username.strip(),
        "passwordHash": hash_password(password),
        "createdAt": now,
        "updatedAt": now,
    }


def _merge_defaults(defaults: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, default_value in defaults.items():
        current_value = current.get(key)
        if isinstance(default_value, dict) and isinstance(current_value, dict):
            merged[key] = _merge_defaults(default_value, current_value)
        elif key in current:
            merged[key] = current_value
        else:
            merged[key] = default_value

    for key, current_value in current.items():
        if key not in merged:
            merged[key] = current_value
    return merged


SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dns_channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    credentials_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS domains (
    id TEXT PRIMARY KEY,
    domain TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    dns_channel_id TEXT NOT NULL,
    expires_at TEXT,
    last_issued_at TEXT,
    last_sync_at TEXT,
    cert_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (dns_channel_id) REFERENCES dns_channels(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ip TEXT NOT NULL DEFAULT '',
    is_online INTEGER NOT NULL DEFAULT 0,
    last_heartbeat_at TEXT,
    cert_dir TEXT NOT NULL DEFAULT '/etc/nginx/ssl',
    last_error TEXT,
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS node_assignments (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    domain_id TEXT NOT NULL,
    desired_sha256 TEXT,
    deployed_sha256 TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    last_deploy_at TEXT,
    expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (node_id, domain_id),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (domain_id) REFERENCES domains(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS node_commands (
    id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    job_id TEXT,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    acked_at TEXT,
    completed_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    target_name TEXT,
    status TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    duration_ms INTEGER,
    error TEXT,
    log_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
"""

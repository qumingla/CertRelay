from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    data_dir: Path
    db_path: Path
    log_dir: Path
    runtime_config_dir: Path
    runtime_tmp_dir: Path
    bundled_acme_home: Path
    secret_key: str
    admin_username: str
    admin_password: str
    token_ttl_seconds: int
    frontend_dist: Path | None
    master_script: Path
    allow_origins: list[str]
    node_heartbeat_interval_seconds: int
    node_offline_missed_heartbeats: int


def load_config() -> AppConfig:
    backend_root = _backend_root()
    default_data_dir = backend_root / ".data"
    default_log_dir = backend_root / ".logs"
    default_frontend_dist = _default_frontend_dist(backend_root)
    default_runtime_config_dir = Path(os.getenv("SSL_SYNC_RUNTIME_CONFIG_DIR", "/etc/ssl-cert-sync")).expanduser()
    default_runtime_tmp_dir = Path(os.getenv("SSL_SYNC_RUNTIME_TMP_DIR", "/tmp/ssl-sync-runtime")).expanduser()
    default_bundled_acme_home = Path(os.getenv("SSL_SYNC_BUNDLED_ACME_HOME", "/opt/acme.sh")).expanduser()

    data_dir = Path(os.getenv("SSL_SYNC_DATA_DIR", str(default_data_dir))).expanduser()
    log_dir = Path(os.getenv("SSL_SYNC_LOG_DIR", str(default_log_dir))).expanduser()
    db_path = Path(os.getenv("SSL_SYNC_DB_PATH", str(data_dir / "ssl-sync.db"))).expanduser()
    runtime_config_dir = Path(os.getenv("SSL_SYNC_RUNTIME_CONFIG_DIR", str(default_runtime_config_dir))).expanduser()
    runtime_tmp_dir = Path(os.getenv("SSL_SYNC_RUNTIME_TMP_DIR", str(default_runtime_tmp_dir))).expanduser()
    bundled_acme_home = Path(os.getenv("SSL_SYNC_BUNDLED_ACME_HOME", str(default_bundled_acme_home))).expanduser()
    frontend_dist_raw = os.getenv("SSL_SYNC_FRONTEND_DIST", str(default_frontend_dist))
    frontend_dist = Path(frontend_dist_raw).expanduser() if frontend_dist_raw else None
    origins = [
        item.strip()
        for item in os.getenv("SSL_SYNC_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
        if item.strip()
    ]

    return AppConfig(
        data_dir=data_dir,
        db_path=db_path,
        log_dir=log_dir,
        runtime_config_dir=runtime_config_dir,
        runtime_tmp_dir=runtime_tmp_dir,
        bundled_acme_home=bundled_acme_home,
        secret_key=os.getenv("SSL_SYNC_SECRET_KEY", "change-me-before-production"),
        admin_username=os.getenv("SSL_SYNC_ADMIN_USERNAME", "admin"),
        admin_password=os.getenv("SSL_SYNC_ADMIN_PASSWORD", "admin"),
        token_ttl_seconds=int(os.getenv("SSL_SYNC_TOKEN_TTL_SECONDS", "86400")),
        frontend_dist=frontend_dist,
        master_script=Path(os.getenv("SSL_SYNC_MASTER_SCRIPT", "/usr/local/bin/cert-master-sync.sh")).expanduser(),
        allow_origins=origins,
        node_heartbeat_interval_seconds=max(1, int(os.getenv("SSL_SYNC_NODE_HEARTBEAT_INTERVAL_SECONDS", "150"))),
        node_offline_missed_heartbeats=max(1, int(os.getenv("SSL_SYNC_NODE_OFFLINE_MISSED_HEARTBEATS", "5"))),
    )


def _backend_root() -> Path:
    explicit = os.getenv("SSL_SYNC_BACKEND_ROOT")
    if explicit:
        return Path(explicit).expanduser()

    # Source checkout: <repo>/web/backend/app/config.py -> <repo>/web/backend
    # Container image:  /app/app/config.py             -> /app
    parents = Path(__file__).resolve().parents
    if len(parents) >= 2:
        return parents[1]
    return Path.cwd()


def _default_frontend_dist(backend_root: Path) -> Path:
    explicit = os.getenv("SSL_SYNC_FRONTEND_DIST")
    if explicit is not None:
        return Path(explicit).expanduser()

    candidates = [
        backend_root / "frontend-dist",                 # container image
        backend_root.parent / "frontend" / "dist",      # web/backend -> web/frontend/dist
        backend_root.parent.parent / "web" / "frontend" / "dist",  # repo root shape
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]

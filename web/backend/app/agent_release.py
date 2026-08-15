from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from functools import lru_cache
from pathlib import Path


SELF_UPDATE_MIN_VERSION = "2026.08.15.1"
AGENT_FILES = (
    ("cert-node-agent.sh", 0o750),
    ("cert-node-pull.sh", 0o750),
    ("cert-puller.service", 0o644),
    ("cert-puller.timer", 0o644),
)
VERSION_PATTERN = re.compile(r'^AGENT_VERSION="([^"]+)"$', re.MULTILINE)


def node_asset_dir() -> Path:
    bundled = Path("/opt/ssl-sync-node")
    if bundled.exists():
        return bundled

    for parent in Path(__file__).resolve().parents:
        if (parent / "cert-node-agent.sh").exists():
            return parent
    return Path.cwd()


@lru_cache(maxsize=1)
def latest_agent_version() -> str:
    source = (node_asset_dir() / "cert-node-agent.sh").read_text(encoding="utf-8")
    match = VERSION_PATTERN.search(source)
    if match is None:
        raise RuntimeError("Unable to determine bundled node agent version")
    return match.group(1)


def supports_self_update(version: str | None) -> bool:
    return _version_key(version) >= _version_key(SELF_UPDATE_MIN_VERSION)


def agent_update_available(current_version: str | None) -> bool:
    return _version_key(latest_agent_version()) > _version_key(current_version)


def build_agent_bundle() -> bytes:
    asset_dir = node_asset_dir()
    files: dict[str, dict[str, str | int]] = {}
    payloads: dict[str, bytes] = {}
    for name, mode in AGENT_FILES:
        payload = (asset_dir / name).read_bytes()
        payloads[name] = payload
        files[name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode": mode,
        }

    manifest = json.dumps(
        {
            "version": latest_agent_version(),
            "minimumSelfUpdateVersion": SELF_UPDATE_MIN_VERSION,
            "files": files,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        _add_bytes(archive, "manifest.json", manifest, 0o644)
        for name, mode in AGENT_FILES:
            _add_bytes(archive, name, payloads[name], mode)
    return buffer.getvalue()


def _version_key(version: str | None) -> tuple[int, ...]:
    values = [int(part) for part in re.findall(r"\d+", str(version or ""))]
    return tuple(values) if values else (0,)


def _add_bytes(archive: tarfile.TarFile, name: str, payload: bytes, mode: int) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mode = mode
    archive.addfile(info, io.BytesIO(payload))

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import signal
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Callable
from urllib import error, request
from uuid import uuid4

from .config import AppConfig
from .db import Database, loads_object, merged_settings
from .timeutil import iso_now


LogWriter = Callable[[str], None]


@dataclass(frozen=True)
class DomainBundle:
    key_file: Path
    chain_file: Path
    sha256: str
    expires_at: str
    work_dir: Path


def webdav_probe(settings: dict[str, Any]) -> None:
    base_url = str(settings.get("url") or "").strip().rstrip("/")
    auth = str(settings.get("auth") or "").strip()
    if not base_url:
        raise ValueError("WebDAV URL is empty")
    if not auth:
        raise ValueError("WebDAV auth is empty")

    probe_name = f".ssl-sync-webdav-probe-{uuid4().hex}.txt"
    probe_url = f"{base_url}/{probe_name}"
    payload = f"ssl-sync probe {probe_name}\n".encode("utf-8")

    put_response = _webdav_request(probe_url, auth, method="PUT", data=payload)
    if put_response["status"] not in {200, 201, 204}:
        raise RuntimeError(f"WebDAV PUT failed with HTTP {put_response['status']}")

    get_response = _webdav_request(probe_url, auth, method="GET")
    if get_response["status"] != 200:
        raise RuntimeError(f"WebDAV GET failed with HTTP {get_response['status']}")
    if get_response["body"] != payload:
        raise RuntimeError("WebDAV GET content mismatch after PUT")

    delete_response = _webdav_request(probe_url, auth, method="DELETE")
    if delete_response["status"] not in {200, 202, 204}:
        raise RuntimeError(f"WebDAV DELETE failed with HTTP {delete_response['status']}")


def upload_domain_bundle(settings: dict[str, Any], domain: str, bundle: DomainBundle) -> None:
    base_url = str(settings.get("url") or "").strip().rstrip("/")
    auth = str(settings.get("auth") or "").strip()
    if not base_url:
        raise ValueError("WebDAV URL is empty")
    if not auth:
        raise ValueError("WebDAV auth is empty")

    sha_file = bundle.work_dir / f"{domain}.sha256"
    sha_file.write_text(f"{bundle.sha256}\n", encoding="utf-8")

    remote_dir = f"{base_url}/{domain}"
    _webdav_request(f"{remote_dir}/", auth, method="MKCOL", ok_statuses={201, 405})
    _upload_file(f"{remote_dir}/{domain}.key", auth, bundle.key_file)
    _upload_file(f"{remote_dir}/{domain}.cer", auth, bundle.chain_file)
    _upload_file(f"{remote_dir}/{domain}.sha256", auth, sha_file)


async def run_domain_script(
    config: AppConfig,
    db: Database,
    domain_id: str,
    *,
    force_reissue: bool = False,
    telegram_enabled: bool = True,
    line_logger: LogWriter | None = None,
) -> DomainBundle:
    domain, settings, channel = _domain_context(db, domain_id)
    runtime_config = _write_runtime_master_config(
        config,
        domain["domain"],
        settings,
        channel,
        telegram_enabled=telegram_enabled,
    )
    args = ["bash", str(config.master_script), "--config", str(runtime_config), "--domain", domain["domain"]]
    if force_reissue:
        args.append("--force-reissue")

    exit_code, output = await _run_process(args, line_logger=line_logger)
    if exit_code != 0:
        raise RuntimeError(_tail_output(output, fallback=f"cert-master-sync exited with code {exit_code}"))

    return await asyncio.to_thread(extract_domain_bundle, config, settings, domain["domain"])


async def test_dns_channel_live(
    config: AppConfig,
    db: Database,
    channel_id: str,
    *,
    line_logger: LogWriter | None = None,
) -> None:
    channel = db.query_one("SELECT * FROM dns_channels WHERE id = ?", (channel_id,))
    if channel is None:
        raise ValueError("DNS channel not found")

    domain = db.query_one(
        "SELECT * FROM domains WHERE dns_channel_id = ? ORDER BY domain LIMIT 1",
        (channel_id,),
    )
    if domain is None:
        raise ValueError("No domain is bound to this DNS channel. Assign a domain first for a real DNS test.")

    settings = _settings(db)
    real_acme_home_text = str(settings.get("acme", {}).get("acmeHome") or "").strip()
    if not real_acme_home_text:
        raise ValueError("ACME Home path is empty")
    real_acme_home = Path(real_acme_home_text).expanduser()
    _ensure_acme_home_seeded(config, real_acme_home)

    temp_acme_home = Path(mkdtemp(prefix="ssl-sync-dns-test-", dir=str(config.runtime_tmp_dir))) / ".acme.sh"
    temp_acme_home.mkdir(parents=True, exist_ok=True)
    shutil.copytree(real_acme_home, temp_acme_home, dirs_exist_ok=True)
    runtime_config = _write_runtime_master_config(
        config,
        domain["domain"],
        settings,
        channel,
        telegram_enabled=False,
        acme_home_override=temp_acme_home,
    )
    args = [
        "bash",
        str(config.master_script),
        "--config",
        str(runtime_config),
        "--domain",
        domain["domain"],
        "--dns-test",
        "--no-telegram",
    ]

    try:
        exit_code, output = await _run_process(args, line_logger=line_logger)
        if exit_code != 0:
            raise RuntimeError(_tail_output(output, fallback=f"dns test exited with code {exit_code}"))
    finally:
        shutil.rmtree(temp_acme_home.parent, ignore_errors=True)


def extract_domain_bundle(config: AppConfig, settings: dict[str, Any], domain: str) -> DomainBundle:
    acme_home_text = str(settings.get("acme", {}).get("acmeHome") or "").strip()
    if not acme_home_text:
        raise ValueError("ACME Home path is empty")
    acme_home = Path(acme_home_text).expanduser()
    _ensure_acme_home_seeded(config, acme_home)
    acme_bin = acme_home / "acme.sh"
    if not acme_bin.exists():
        raise RuntimeError(f"acme.sh not found at {acme_bin}")

    work_dir = Path(mkdtemp(prefix=f"ssl-sync-bundle-{domain.replace('.', '-')}-"))
    key_file = work_dir / f"{domain}.key"
    chain_file = work_dir / f"{domain}.cer"

    completed = subprocess.run(
        [
            str(acme_bin),
            "--install-cert",
            "-d",
            f"*.{domain}",
            "--home",
            str(acme_home),
            "--key-file",
            str(key_file),
            "--fullchain-file",
            str(chain_file),
        ],
        check=False,
        timeout=120,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(_tail_output(completed.stdout + completed.stderr, fallback="Failed to export certificate bundle"))
    if not key_file.exists() or not chain_file.exists():
        raise RuntimeError("acme.sh export completed but certificate files are missing")

    sha256 = hashlib.sha256(chain_file.read_bytes()).hexdigest()
    expires_at = _read_certificate_expiry(chain_file)
    return DomainBundle(key_file=key_file, chain_file=chain_file, sha256=sha256, expires_at=expires_at, work_dir=work_dir)


def cleanup_bundle(bundle: DomainBundle | None) -> None:
    if bundle is None:
        return
    shutil.rmtree(bundle.work_dir, ignore_errors=True)


def update_domain_state(
    db: Database,
    domain_id: str,
    bundle: DomainBundle,
    *,
    mark_issued: bool = False,
    mark_synced: bool = False,
) -> None:
    now = iso_now()
    last_issued_at = now if mark_issued else None
    last_sync_at = now if mark_synced else None

    row = db.query_one("SELECT * FROM domains WHERE id = ?", (domain_id,))
    if row is None:
        raise ValueError("Domain not found")

    db.execute(
        """
        UPDATE domains
        SET cert_sha256 = ?,
            expires_at = ?,
            last_issued_at = COALESCE(?, last_issued_at),
            last_sync_at = COALESCE(?, last_sync_at),
            status = 'active',
            last_error = NULL,
            updated_at = ?
        WHERE id = ?
        """,
        (bundle.sha256, bundle.expires_at, last_issued_at, last_sync_at, now, domain_id),
    )
    db.execute(
        """
        UPDATE node_assignments
        SET desired_sha256 = ?,
            expires_at = ?,
            status = CASE WHEN deployed_sha256 = ? THEN 'synced' ELSE 'pending' END,
            last_error = NULL,
            updated_at = ?
        WHERE domain_id = ?
        """,
        (bundle.sha256, bundle.expires_at, bundle.sha256, now, domain_id),
    )


def mark_domain_error(db: Database, domain_id: str, error_message: str) -> None:
    db.execute(
        "UPDATE domains SET status = 'error', last_error = ?, updated_at = ? WHERE id = ?",
        (error_message, iso_now(), domain_id),
    )


async def _run_process(args: list[str], *, line_logger: LogWriter | None = None) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    from collections import deque
    output_lines: deque[str] = deque(maxlen=200)
    try:
        async with asyncio.timeout(1800):
            assert process.stdout is not None
            while raw_line := await process.stdout.readline():
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                output_lines.append(line)
                if line_logger is not None:
                    line_logger(line)
            return_code = await process.wait()
        return return_code, "\n".join(output_lines)
    finally:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except TimeoutError:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()


def _settings(db: Database) -> dict[str, Any]:
    row = db.query_one("SELECT value FROM app_settings WHERE key = 'settings'")
    return merged_settings(row["value"] if row else "{}")


def _domain_context(db: Database, domain_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    domain = db.query_one("SELECT * FROM domains WHERE id = ?", (domain_id,))
    if domain is None:
        raise ValueError("Domain not found")

    settings = _settings(db)
    channel = db.query_one("SELECT * FROM dns_channels WHERE id = ?", (domain["dns_channel_id"],))
    if channel is None:
        raise ValueError("DNS channel not found")
    return domain, settings, channel


def _write_runtime_master_config(
    config: AppConfig,
    domain: str,
    settings: dict[str, Any],
    channel: dict[str, Any],
    *,
    telegram_enabled: bool,
    acme_home_override: Path | None = None,
) -> Path:
    acme_settings = settings.get("acme", {})
    webdav_settings = settings.get("webdav", {})
    telegram_settings = settings.get("telegram", {})
    credentials = loads_object(channel.get("credentials_json"))

    runtime_path = config.runtime_config_dir / f"acme-master-{domain.replace('.', '_')}.conf"
    staging_base = config.runtime_tmp_dir / f"staging-{domain.replace('.', '-')}"
    if acme_home_override is not None:
        acme_home = acme_home_override
    else:
        acme_home_text = str(acme_settings.get("acmeHome") or "").strip()
        if not acme_home_text:
            raise ValueError("ACME Home path is empty")
        acme_home = Path(acme_home_text).expanduser()

    lines = [
        f'DOMAINS=({_shell_quote(domain)})',
        f'DNS_PROVIDER={_shell_quote(str(channel["provider"]))}',
        f'TELEGRAM_ENABLED={_shell_quote("1" if telegram_enabled else "0")}',
        f'TG_BOT_TOKEN={_shell_quote(str(telegram_settings.get("botToken") or ""))}',
        f'TG_CHAT_ID={_shell_quote(str(telegram_settings.get("chatId") or ""))}',
        f'WEBDAV_URL={_shell_quote(str(webdav_settings.get("url") or ""))}',
        f'WEBDAV_AUTH={_shell_quote(str(webdav_settings.get("auth") or ""))}',
        f'ACME_HOME={_shell_quote(str(acme_home))}',
        f'BUNDLED_ACME_HOME={_shell_quote(str(config.bundled_acme_home))}',
        f'STAGING_BASE={_shell_quote(str(staging_base))}',
        f'LOG_FILE={_shell_quote(str(config.log_dir / "cert-master-sync.log"))}',
        f'RENEW_DAYS_BEFORE={_shell_quote(str(acme_settings.get("defaultRenewDays") or 7))}',
        f'ACME_SERVER={_shell_quote(str(acme_settings.get("defaultCa") or "letsencrypt"))}',
        f'ACME_ACCOUNT_EMAIL={_shell_quote(str(acme_settings.get("accountEmail") or ""))}',
    ]
    for key, value in sorted(credentials.items()):
        lines.append(f"{key}={_shell_quote(str(value))}")

    runtime_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    runtime_path.chmod(0o600)
    return runtime_path


def _read_certificate_expiry(chain_file: Path) -> str:
    completed = subprocess.run(
        ["openssl", "x509", "-noout", "-enddate", "-in", str(chain_file)],
        check=False,
        timeout=120,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("Failed to read certificate expiry with openssl")
    raw = completed.stdout.strip()
    if "=" not in raw:
        raise RuntimeError(f"Unexpected openssl enddate output: {raw}")
    value = raw.split("=", 1)[1].strip()
    expires_at = datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    return expires_at.isoformat().replace("+00:00", "Z")


def _upload_file(url: str, auth: str, file_path: Path) -> None:
    response = _webdav_request(url, auth, method="PUT", data=file_path.read_bytes())
    if response["status"] not in {200, 201, 204}:
        raise RuntimeError(f"WebDAV upload failed for {url}: HTTP {response['status']}")


def _webdav_request(
    url: str,
    auth: str,
    *,
    method: str,
    data: bytes | None = None,
    ok_statuses: set[int] | None = None,
) -> dict[str, Any]:
    username, password = _split_auth(auth)
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    req = request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/octet-stream",
        },
    )

    try:
        with request.urlopen(req, timeout=20) as response:
            body = response.read()
            status_code = response.getcode()
    except error.HTTPError as exc:
        body = exc.read()
        status_code = exc.code
        if ok_statuses and status_code in ok_statuses:
            return {"status": status_code, "body": body}
        detail = body.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"WebDAV {method} {url} failed: HTTP {status_code} {detail}".strip()) from exc
    except error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"WebDAV {method} {url} failed: {reason}") from exc

    if ok_statuses and status_code not in ok_statuses:
        raise RuntimeError(f"WebDAV {method} {url} returned HTTP {status_code}")
    return {"status": status_code, "body": body}


def _split_auth(auth: str) -> tuple[str, str]:
    if ":" not in auth:
        raise ValueError("WebDAV auth must be in user:password format")
    return auth.split(":", 1)


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _tail_output(output: str, *, fallback: str) -> str:
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return fallback
    return "\n".join(lines[-20:])


def _ensure_acme_home_seeded(config: AppConfig, acme_home: Path) -> None:
    acme_bin = acme_home / "acme.sh"
    if acme_bin.exists():
        return

    bundled_home = config.bundled_acme_home
    bundled_bin = bundled_home / "acme.sh"
    if not bundled_bin.exists():
        raise RuntimeError(f"acme.sh not found at {acme_bin}, and bundled fallback is missing at {bundled_bin}")

    acme_home.mkdir(parents=True, exist_ok=True)
    for source in bundled_home.rglob("*"):
        relative = source.relative_to(bundled_home)
        destination = acme_home / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    if not acme_bin.exists():
        raise RuntimeError(f"Failed to bootstrap acme.sh into {acme_home}")

    acme_bin.chmod(0o755)

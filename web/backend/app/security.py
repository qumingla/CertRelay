from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import TYPE_CHECKING, Any

from fastapi import Header, HTTPException, Query, Request, status

from .config import AppConfig
if TYPE_CHECKING:
    from .db import Database


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def create_signed_token(
    config: AppConfig,
    subject: str,
    kind: str,
    ttl_seconds: int | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    expires_at = int(time.time()) + (ttl_seconds if ttl_seconds is not None else config.token_ttl_seconds)
    payload: dict[str, Any] = {
        "sub": subject,
        "kind": kind,
        "exp": expires_at,
    }
    if extra:
        payload.update(extra)
    body = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = hmac.new(config.secret_key.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_signed_token(config: AppConfig, token: str, kind: str | None = None) -> dict[str, Any] | None:
    if "." not in token:
        return None
    body, signature = token.rsplit(".", 1)
    expected = hmac.new(config.secret_key.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        payload = json.loads(_b64_decode(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if kind is not None and payload.get("kind") != kind:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, int) or exp < int(time.time()):
        return None
    return payload


def hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_password(raw: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(raw.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${_b64_encode(salt)}${_b64_encode(digest)}"


def verify_password_hash(stored: str, raw: str) -> bool:
    try:
        scheme, salt_encoded, digest_encoded = stored.split("$", 2)
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    try:
        salt = _b64_decode(salt_encoded)
        expected = _b64_decode(digest_encoded)
    except Exception:
        return False
    actual = hashlib.scrypt(raw.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=len(expected))
    return hmac.compare_digest(actual, expected)


def new_node_token() -> str:
    return f"node_{secrets.token_urlsafe(32)}"


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def require_admin(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> dict[str, Any]:
    config: AppConfig = request.app.state.config
    raw_token = _extract_bearer(authorization) or token
    if raw_token:
        payload = verify_signed_token(config, raw_token, kind="admin")
        if payload is not None:
            return payload
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "Unauthorized"})


async def require_node(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    db: Database = request.app.state.db
    raw_token = _extract_bearer(authorization)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "Unauthorized"})
    token_hash = hash_secret(raw_token)
    node = db.query_one("SELECT * FROM nodes WHERE token_hash = ?", (token_hash,))
    if node is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "Unauthorized"})
    return node

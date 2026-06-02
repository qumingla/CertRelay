from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..config import AppConfig
from ..db import Database, dumps, merged_auth_state
from ..schemas import AccountUpdateRequest, AuthAccountResponse, AuthStatusResponse, BootstrapRequest, LoginRequest
from ..security import create_signed_token, hash_password, require_admin, verify_password_hash
from ..timeutil import iso_now

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _load_auth_state(db: Database) -> dict[str, object]:
    row = db.query_one("SELECT value FROM app_settings WHERE key = 'auth'")
    return merged_auth_state(row["value"] if row else None)


def _build_status(state: dict[str, object]) -> AuthStatusResponse:
    initialized = bool(state.get("initialized"))
    return AuthStatusResponse(initialized=initialized, setupRequired=not initialized)


@router.get("/status", response_model=AuthStatusResponse)
async def auth_status(request: Request) -> AuthStatusResponse:
    db: Database = request.app.state.db
    return _build_status(_load_auth_state(db))


@router.get("/account", response_model=AuthAccountResponse)
async def auth_account(
    request: Request,
    _admin: dict[str, object] = Depends(require_admin),
) -> AuthAccountResponse:
    db: Database = request.app.state.db
    state = _load_auth_state(db)
    return AuthAccountResponse(username=str(state.get("username") or ""))


@router.post("/bootstrap")
async def bootstrap(payload: BootstrapRequest, request: Request) -> dict[str, str]:
    db: Database = request.app.state.db
    config: AppConfig = request.app.state.config
    current = _load_auth_state(db)
    if bool(current.get("initialized")):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "Initial setup has already been completed", "setupRequired": False},
        )

    username = payload.username.strip()
    password = payload.password
    if not username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Username is required"})
    if len(password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "Password must be at least 8 characters long"},
        )

    now = iso_now()
    auth_state = {
        "initialized": True,
        "username": username,
        "passwordHash": hash_password(password),
        "createdAt": now,
        "updatedAt": now,
    }
    db.execute(
        "UPDATE app_settings SET value = ?, updated_at = ? WHERE key = 'auth'",
        (dumps(auth_state), now),
    )
    token = create_signed_token(config, subject=username, kind="admin")
    return {"token": token}


@router.patch("/account", response_model=AuthAccountResponse)
async def update_account(
    payload: AccountUpdateRequest,
    request: Request,
    _admin: dict[str, object] = Depends(require_admin),
) -> AuthAccountResponse:
    db: Database = request.app.state.db
    auth_state = _load_auth_state(db)
    if not bool(auth_state.get("initialized")):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "Initial setup is required", "setupRequired": True},
        )

    username = payload.username.strip()
    current_password = payload.currentPassword
    new_password = payload.newPassword
    if not username:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Username is required"})
    if not current_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": "Current password is required"})

    stored_username = str(auth_state.get("username") or "")
    stored_password_hash = str(auth_state.get("passwordHash") or "")
    if not verify_password_hash(stored_password_hash, current_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "Current password is incorrect"},
        )

    if not new_password and username == stored_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "No account changes were provided"},
        )

    if new_password and len(new_password) < 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "New password must be at least 8 characters long"},
        )

    now = iso_now()
    auth_state.update(
        {
            "initialized": True,
            "username": username,
            "passwordHash": hash_password(new_password) if new_password else stored_password_hash,
            "updatedAt": now,
        }
    )
    if not auth_state.get("createdAt"):
        auth_state["createdAt"] = now
    db.execute(
        "UPDATE app_settings SET value = ?, updated_at = ? WHERE key = 'auth'",
        (dumps(auth_state), now),
    )
    return AuthAccountResponse(username=username)


@router.post("/login")
async def login(payload: LoginRequest, request: Request) -> dict[str, str]:
    db: Database = request.app.state.db
    config: AppConfig = request.app.state.config
    auth_state = _load_auth_state(db)
    if not bool(auth_state.get("initialized")):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "Initial setup is required", "setupRequired": True},
        )

    stored_username = str(auth_state.get("username") or "")
    stored_password_hash = str(auth_state.get("passwordHash") or "")
    valid_username = secrets.compare_digest(payload.username, stored_username)
    valid_password = verify_password_hash(stored_password_hash, payload.password)
    if not (valid_username and valid_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"error": "Invalid credentials"})
    token = create_signed_token(config, subject=stored_username, kind="admin")
    return {"token": token}

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .db import Database
from .events import EventHub
from .routers import admin, auth, node


def create_app() -> FastAPI:
    config = load_config()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    config.runtime_config_dir.mkdir(parents=True, exist_ok=True)
    config.runtime_tmp_dir.mkdir(parents=True, exist_ok=True)

    db = Database(config.db_path)
    db.init(config.admin_username, config.admin_password)

    app = FastAPI(
        title="SSL Certificate Sync Master API",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.state.config = config
    app.state.db = db
    app.state.event_hub = EventHub(db)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Request, exc: HTTPException):
        if isinstance(exc.detail, dict):
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"error": str(exc.detail)})

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(node.router)
    app.include_router(node.bootstrap_router)

    _mount_frontend(app, config.frontend_dist)
    return app


def _mount_frontend(app: FastAPI, dist_dir: Path | None) -> None:
    if dist_dir is None or not dist_dir.exists():
        return
    assets = dist_dir / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

    index_html = dist_dir / "index.html"
    if not index_html.exists():
        return

    @app.get("/")
    async def frontend_index():
        return FileResponse(index_html)

    @app.get("/{path:path}")
    async def frontend_spa(path: str):
        if path.startswith("api/"):
            return JSONResponse(status_code=404, content={"error": "Not found"})
        candidate = dist_dir / path
        if candidate.exists() and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index_html)


app = create_app()

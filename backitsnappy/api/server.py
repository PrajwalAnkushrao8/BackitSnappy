"""FastAPI app factory and the local-only uvicorn server setup.

One `uvicorn.Server` instance, bound to 127.0.0.1 only, for the local UI.
Never binds 0.0.0.0 or any other interface -- this app has no remote/network
upload path (that was Tailscale-based iPhone Shortcuts support, removed).

Auth is applied per-router rather than app-wide: most /api/* routes require
a valid X-Pairing-Token header (verify_pairing_token). The two media-serving
routes (thumbnail/full media) additionally accept the token as a ?token=
query param (verify_pairing_token_flexible), since browsers can't attach
custom headers to <img>/<video> src requests — see api/deps.py. The static
UI shell is unauthenticated, since it contains no secrets.
"""
import asyncio
import logging
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import config
from ..telegram.client_manager import TelegramManager
from . import (
    routes_albums,
    routes_files,
    routes_media,
    routes_photos_backup,
    routes_settings,
    routes_setup,
    routes_upload,
)
from .deps import verify_pairing_token, verify_pairing_token_flexible

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "ui" / "static"


class _Server(uvicorn.Server):
    """uvicorn.Server that never installs signal handlers — required when
    running outside the main thread."""

    def install_signal_handlers(self) -> None:
        pass


def create_app(manager: TelegramManager) -> FastAPI:
    app = FastAPI(title="BackitSnappy")
    app.state.telegram_manager = manager

    # Rejects requests whose Host header isn't a loopback name, which is what
    # closes DNS rebinding: binding to 127.0.0.1 stops other machines from
    # reaching this socket, but it does nothing about a page on some public
    # site whose domain has been re-pointed at 127.0.0.1 -- to the browser
    # that page is then same-origin with this server, so its requests carry
    # no CORS restriction. Checking Host means such a request (which still
    # carries the attacker's domain) never reaches a route.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    strict = [Depends(verify_pairing_token)]
    app.include_router(routes_setup.router, prefix="/api/setup", tags=["setup"], dependencies=strict)
    app.include_router(routes_upload.router, prefix="/api", tags=["upload"], dependencies=strict)
    app.include_router(routes_albums.router, prefix="/api/albums", tags=["albums"], dependencies=strict)
    app.include_router(routes_files.router, prefix="/api/files", tags=["files"], dependencies=strict)
    app.include_router(routes_settings.router, prefix="/api/settings", tags=["settings"], dependencies=strict)
    app.include_router(
        routes_photos_backup.router, prefix="/api/photos_backup", tags=["photos_backup"], dependencies=strict
    )
    app.include_router(
        routes_media.router, prefix="/api/files", tags=["media"],
        dependencies=[Depends(verify_pairing_token_flexible)],
    )

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


async def run_servers(app: FastAPI, stop_event: asyncio.Event, on_local_ready=None) -> None:
    """Runs the local-only listener until stop_event is set.

    `on_local_ready` (if given) fires once the local socket is actually bound
    and accepting connections — the caller uses this to know it's safe to
    point the pywebview window at the local URL, avoiding a startup race.
    """
    cfg = config.load()
    local_server = _Server(
        uvicorn.Config(app, host="127.0.0.1", port=cfg["local_port"], log_level="warning")
    )
    task = asyncio.create_task(local_server.serve())
    while not local_server.started:
        await asyncio.sleep(0.02)
    logger.info("Local API listening on http://127.0.0.1:%s", cfg["local_port"])
    if on_local_ready:
        on_local_ready()

    await stop_event.wait()
    local_server.should_exit = True
    await asyncio.gather(task, return_exceptions=True)

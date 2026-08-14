"""FastAPI app factory and the dual-bind uvicorn server setup.

Two `uvicorn.Server` instances share one FastAPI `app`: one always bound to
127.0.0.1 for the local UI, and one — only started if the user has opted in
via Settings — bound to the discovered Tailscale IP for iPhone Shortcuts
uploads. Neither ever binds 0.0.0.0.

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

from .. import config
from ..network import tailscale
from ..telegram.client_manager import TelegramManager
from . import routes_albums, routes_files, routes_media, routes_settings, routes_setup, routes_upload
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

    strict = [Depends(verify_pairing_token)]
    app.include_router(routes_setup.router, prefix="/api/setup", tags=["setup"], dependencies=strict)
    app.include_router(routes_upload.router, prefix="/api", tags=["upload"], dependencies=strict)
    app.include_router(routes_albums.router, prefix="/api/albums", tags=["albums"], dependencies=strict)
    app.include_router(routes_files.router, prefix="/api/files", tags=["files"], dependencies=strict)
    app.include_router(routes_settings.router, prefix="/api/settings", tags=["settings"], dependencies=strict)
    app.include_router(
        routes_media.router, prefix="/api/files", tags=["media"],
        dependencies=[Depends(verify_pairing_token_flexible)],
    )

    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


async def run_servers(app: FastAPI, stop_event: asyncio.Event, on_local_ready=None) -> None:
    """Start the local listener always, and the Tailscale listener if enabled
    in settings. Runs until stop_event is set. The Tailscale toggle is read
    once at startup; changing it in Settings takes effect on next launch.

    `on_local_ready` (if given) fires once the local socket is actually bound
    and accepting connections — the caller uses this to know it's safe to
    point the pywebview window at the local URL, avoiding a startup race.
    """
    cfg = config.load()
    servers: list[uvicorn.Server] = []
    tasks: list[asyncio.Task] = []

    local_server = _Server(
        uvicorn.Config(app, host="127.0.0.1", port=cfg["local_port"], log_level="warning")
    )
    servers.append(local_server)
    tasks.append(asyncio.create_task(local_server.serve()))
    while not local_server.started:
        await asyncio.sleep(0.02)
    logger.info("Local API listening on http://127.0.0.1:%s", cfg["local_port"])
    if on_local_ready:
        on_local_ready()

    if cfg["tailscale_access_enabled"]:
        ts_ip = tailscale.discover_tailscale_ipv4()
        if ts_ip:
            ts_server = _Server(
                uvicorn.Config(app, host=ts_ip, port=cfg["tailscale_port"], log_level="warning")
            )
            servers.append(ts_server)
            tasks.append(asyncio.create_task(ts_server.serve()))
            logger.info("Tailscale API listening on http://%s:%s", ts_ip, cfg["tailscale_port"])
        else:
            logger.warning("Tailscale access enabled in settings but no Tailscale IP was found")

    await stop_event.wait()
    for server in servers:
        server.should_exit = True
    await asyncio.gather(*tasks, return_exceptions=True)

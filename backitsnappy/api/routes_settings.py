"""Non-secret settings only. The pairing token itself is never served over
HTTP (even authenticated) — the UI reads/rotates it via the pywebview JS
bridge (window.py), which is local-process-only and never reachable over
Tailscale. This keeps the one secret that gates network access off the
network layer entirely.
"""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import config

router = APIRouter()


class WatchFolderIn(BaseModel):
    path: str | None


class TailscaleAccessIn(BaseModel):
    enabled: bool


@router.get("")
async def get_settings():
    cfg = config.load()
    return {
        "watch_folder": cfg["watch_folder"],
        "tailscale_access_enabled": cfg["tailscale_access_enabled"],
        "local_port": cfg["local_port"],
        "tailscale_port": cfg["tailscale_port"],
        "storage_channel_id": cfg["storage_channel_id"],
    }


@router.put("/watch_folder")
async def set_watch_folder(body: WatchFolderIn, request: Request):
    if body.path is not None and not Path(body.path).is_dir():
        raise HTTPException(status_code=400, detail="Not a directory")
    try:
        request.app.state.watcher_controller.restart(body.path)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Could not watch folder: {exc}") from exc
    config.set("watch_folder", body.path)
    return {"watch_folder": body.path}


@router.put("/tailscale_access")
async def set_tailscale_access(body: TailscaleAccessIn):
    config.set("tailscale_access_enabled", body.enabled)
    return {"tailscale_access_enabled": body.enabled, "restart_required": True}

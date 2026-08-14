"""Non-secret settings only. The pairing token itself is never served over
HTTP (even authenticated) — the UI reads/rotates it via the pywebview JS
bridge (window.py), which is local-process-only and never reachable over
Tailscale. This keeps the one secret that gates network access off the
network layer entirely.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import config
from ..network import tailscale
from ..telegram.client_manager import TelegramManager
from .deps import get_manager

router = APIRouter()


class WatchFolderIn(BaseModel):
    path: str | None


class TailscaleAccessIn(BaseModel):
    enabled: bool


class OnboardingIn(BaseModel):
    completed: bool


@router.get("")
async def get_settings():
    cfg = config.load()
    return {
        "watch_folder": cfg["watch_folder"],
        "tailscale_access_enabled": cfg["tailscale_access_enabled"],
        "local_port": cfg["local_port"],
        "tailscale_port": cfg["tailscale_port"],
        "storage_channel_id": cfg["storage_channel_id"],
        "iphone_backup_album_id": cfg["iphone_backup_album_id"],
        "onboarding_completed": cfg["onboarding_completed"],
    }


@router.get("/tailscale_url")
async def get_tailscale_url():
    """On-demand re-detection (also used at startup) of this Mac's current
    Tailscale IP, assembled into the full upload URL the Shortcut needs.
    available=False (ip/upload_url null) when Tailscale isn't installed or
    isn't running -- not an error, just nothing to show yet."""
    cfg = config.load()
    ip = tailscale.discover_tailscale_ipv4()
    if ip is None:
        return {"available": False, "ip": None, "upload_url": None}
    return {
        "available": True,
        "ip": ip,
        "upload_url": f"http://{ip}:{cfg['tailscale_port']}/api/upload",
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


@router.put("/onboarding_completed")
async def set_onboarding_completed(body: OnboardingIn):
    config.set("onboarding_completed", body.completed)
    return {"onboarding_completed": body.completed}


@router.post("/sync")
async def sync_with_telegram(manager: TelegramManager = Depends(get_manager)):
    """Reconciles the local index against Telegram -- removes entries for
    messages/channels deleted directly in Telegram, outside the app."""
    try:
        return await manager.sync_all()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

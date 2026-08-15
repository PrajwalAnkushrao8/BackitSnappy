"""Non-secret settings only. The pairing token itself is never served over
HTTP (even authenticated) — the UI reads/rotates it via the pywebview JS
bridge (window.py), which is local-process-only. This keeps the one secret
that gates API access off the network layer entirely.
"""
import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .. import config
from ..telegram.client_manager import TelegramManager
from .deps import get_manager

router = APIRouter()


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _compute_disk_usage() -> dict:
    """Runs in a thread (see the route below) -- rglob over the media
    cache can mean thousands of stat() calls for a well-used install, and
    this shouldn't block the event loop uploads/downloads/sync all share."""
    base = config.APP_SUPPORT_DIR
    database_bytes = sum(f.stat().st_size for f in base.glob("backitsnappy.db*") if f.is_file())
    thumbnails_bytes = _dir_size(base / "thumbnails")
    media_cache_bytes = _dir_size(base / "media_cache")
    return {
        "total_bytes": database_bytes + thumbnails_bytes + media_cache_bytes,
        "database_bytes": database_bytes,
        "thumbnails_bytes": thumbnails_bytes,
        "media_cache_bytes": media_cache_bytes,
        "max_bytes": config.get("media_cache_max_bytes"),
    }


class WatchFolderIn(BaseModel):
    path: str | None


class OnboardingIn(BaseModel):
    completed: bool


class MediaCacheMaxIn(BaseModel):
    gigabytes: float


@router.get("")
async def get_settings():
    cfg = config.load()
    return {
        "watch_folder": cfg["watch_folder"],
        "local_port": cfg["local_port"],
        "storage_channel_id": cfg["storage_channel_id"],
        "iphone_backup_album_id": cfg["iphone_backup_album_id"],
        "onboarding_completed": cfg["onboarding_completed"],
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


@router.get("/disk_usage")
async def get_disk_usage():
    return await asyncio.to_thread(_compute_disk_usage)


@router.put("/media_cache_max_bytes")
async def set_media_cache_max(body: MediaCacheMaxIn):
    if not (1 <= body.gigabytes <= 200):
        raise HTTPException(status_code=400, detail="Cache limit must be between 1 and 200 GB")
    max_bytes = int(body.gigabytes * 1024**3)
    config.set("media_cache_max_bytes", max_bytes)
    return {"max_bytes": max_bytes}


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

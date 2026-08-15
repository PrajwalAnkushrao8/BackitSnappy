"""Automatic Photos Backup settings, permission status, and audit log.

Enabling is a separate, explicit action from anything else -- the
confirmation dialog itself lives in the frontend (same pattern as the
feature this replaced). This router never calls AppleScript itself except
for the read-only permission probe -- the actual poll/export/upload/delete
pipeline lives in photos_backup.py's background loop.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import config, db, photos_automation, photos_backup

router = APIRouter()


class PollIntervalIn(BaseModel):
    minutes: int


class EnableIn(BaseModel):
    enabled: bool


@router.get("/settings")
async def get_photos_backup_settings():
    return {
        "enabled": config.get("photos_backup_enabled"),
        "poll_interval_minutes": config.get("photos_backup_poll_interval_minutes"),
        "last_checked_at": db.get_photos_backup_last_checked(),
        "backed_up_count": db.count_photos_backup_log(),
    }


@router.put("/poll_interval")
async def set_poll_interval(body: PollIntervalIn):
    if not (5 <= body.minutes <= 120):
        raise HTTPException(status_code=400, detail="Poll interval must be between 5 and 120 minutes")
    config.set("photos_backup_poll_interval_minutes", body.minutes)
    return {"poll_interval_minutes": body.minutes}


@router.put("/enable")
async def set_enabled(body: EnableIn):
    config.set("photos_backup_enabled", body.enabled)
    return {"enabled": body.enabled}


@router.get("/status")
async def get_status():
    """Live "is a poll cycle actively running right now" indicator for
    Settings -- see photos_backup.get_status()."""
    return photos_backup.get_status()


@router.get("/permission_status")
async def get_permission_status():
    status = await photos_automation.check_permission()
    return {"status": status}


@router.get("/log")
async def get_photos_backup_log(limit: int = 50):
    rows = db.list_photos_backup_log(limit=limit)
    return [dict(row) for row in rows]

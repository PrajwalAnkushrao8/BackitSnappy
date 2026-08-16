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


class DeleteAfterDaysIn(BaseModel):
    days: int


@router.get("/settings")
async def get_photos_backup_settings():
    return {
        "enabled": config.get("photos_backup_enabled"),
        "poll_interval_minutes": config.get("photos_backup_poll_interval_minutes"),
        "delete_after_days": config.get("photos_backup_delete_after_days"),
        "last_checked_at": db.get_photos_backup_last_checked(),
        "backed_up_count": db.count_photos_backup_log(),
        # Backed up and still sitting in the Photos library -- what the
        # manual "free up space now" action below would clear.
        "pending_deletion_count": db.count_photos_backup_pending_deletion(),
    }


@router.put("/delete_after_days")
async def set_delete_after_days(body: DeleteAfterDaysIn):
    """How old an item must be before it's removed from the Photos library.
    Backup itself is never delayed by this -- everything uploads as soon as
    it's found; this only gates the delete."""
    if not (0 <= body.days <= 3650):
        raise HTTPException(status_code=400, detail="Delete delay must be between 0 and 3650 days")
    config.set("photos_backup_delete_after_days", body.days)
    return {"delete_after_days": body.days}


@router.post("/delete_now")
async def delete_now():
    """Manual "free up iCloud storage now": deletes every backed-up item
    still in the Photos library, ignoring the age window. macOS will show
    its own confirmation dialog before anything is removed."""
    deleted = await photos_backup.delete_all_backed_up_now()
    return {"deleted": deleted}


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

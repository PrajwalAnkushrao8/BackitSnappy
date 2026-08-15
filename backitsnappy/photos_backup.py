"""Orchestrates the Automatic Photos Backup feature: poll Photos.app for
new items, export -> upload -> (only after a confirmed Telegram response)
delete via Photos' own `delete` command, log it.

Pure asyncio, no threads -- unlike the folder watchers this replaced,
there's no filesystem to watch; it's just periodic subprocess calls
(photos_automation.py) plus the existing upload pipeline.

Upload completion is observed by polling the same job dict the frontend's
own progress bar polls (manager.jobs[job_id], see api/routes_upload.py) --
deliberately not client_manager's on_confirmed callback mechanism, since
that would need a sync-callback-schedules-async-task bridge (delete_item is
a subprocess call) for no real benefit: this module already needs job["status"]
and job["file_id"], both already used elsewhere for exactly this. A file is
only ever considered "safely backed up" once job["status"] == "done" and a
real files.id row is confirmed present.
"""
import asyncio
import logging
import shutil
import tempfile
import time
from pathlib import Path

from . import config, db, photos_automation
from .telegram.client_manager import TelegramManager

logger = logging.getLogger(__name__)

# How often the loop wakes up to check whether it's time to poll -- short,
# so enabling the feature or changing the poll interval in Settings takes
# effect promptly rather than waiting out whatever interval was previously
# configured.
POLL_TICK_SECONDS = 30
UPLOAD_POLL_INTERVAL_SECONDS = 0.5

# Items currently mid-export/upload/delete -- guards against a poll cycle
# re-discovering an item as "new" while it's still in flight (it isn't in
# photos_backup_log yet, since that only gets written once fully done).
_in_flight: set[str] = set()

# Live progress for the Settings UI -- "is a poll cycle actively running
# right now, and how far through it." A plain module-level dict rather than
# anything durable: this is a cosmetic in-progress indicator, not something
# that needs to survive a restart.
_status = {"active": False, "total": 0, "done": 0}


def get_status() -> dict:
    return dict(_status)


async def _process_item(manager: TelegramManager, album_id: int, item_id: str, filename: str) -> None:
    _in_flight.add(item_id)
    temp_dir = Path(tempfile.mkdtemp(prefix="backitsnappy-photos-"))
    try:
        # Usually one file, but a Live Photo's original is genuinely two
        # (the still + its paired .mov) -- every component must be
        # confirmed-uploaded before the item is deleted, or deleting it
        # would discard whichever part wasn't backed up.
        exported_paths = await photos_automation.export_item(item_id, temp_dir)

        file_rows = []
        for path in exported_paths:
            job_id = manager.start_upload(path, album_id=album_id, source="photos_backup")
            job = manager.jobs[job_id]
            while job["status"] not in ("done", "error"):
                await asyncio.sleep(UPLOAD_POLL_INTERVAL_SECONDS)

            if job["status"] != "done":
                logger.warning(
                    "Photos backup upload failed for %s (%s), leaving it in Photos: %s",
                    filename, path.name, job.get("error"),
                )
                return

            file_row = db.get_file(job["file_id"])
            if file_row is None:
                logger.warning(
                    "Photos backup: uploaded file row vanished for %s (%s) before delete",
                    filename, path.name,
                )
                return
            file_rows.append(file_row)

        # Only reachable once every exported component has a real files.id
        # row -- each only ever gets inserted after Telegram actually
        # returned a message id (fresh, forwarded, or deduped-existing; see
        # client_manager._do_upload). Deleting from Photos here is safe.
        await photos_automation.delete_item(item_id)
        now = time.time()
        for file_row in file_rows:
            db.insert_photos_backup_log(
                photos_item_id=item_id,
                filename=file_row["filename"],
                size=file_row["size"],
                telegram_message_id=file_row["telegram_message_id"],
                channel_id=file_row["channel_id"],
                uploaded_at=now,
                deleted_at=now,
            )
        logger.info(
            "Photos backup: backed up (%d file(s)) and deleted %s (item %s)",
            len(file_rows), filename, item_id,
        )
    except Exception:
        logger.exception("Photos backup failed for item %s (%s)", item_id, filename)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        _in_flight.discard(item_id)
        _status["done"] += 1


async def poll_cycle(manager: TelegramManager) -> None:
    status = await photos_automation.check_permission()
    if status != "granted":
        logger.info("Skipping Photos backup poll cycle: permission status is %s", status)
        return

    try:
        items = await photos_automation.list_all_item_ids()
    except (asyncio.TimeoutError, photos_automation.PhotosAutomationError):
        logger.exception("Failed to list Photos library items")
        return

    known = db.get_processed_photos_item_ids()
    new_items = [
        (item_id, filename) for item_id, filename in items
        if item_id not in known and item_id not in _in_flight
    ]
    if not new_items:
        return
    logger.info("Photos backup: found %d new item(s)", len(new_items))
    album_id = await manager.ensure_photos_backup_album()
    _status.update(active=True, total=len(new_items), done=0)
    try:
        await asyncio.gather(
            *(_process_item(manager, album_id, item_id, filename) for item_id, filename in new_items)
        )
    finally:
        _status["active"] = False


async def poll_loop(manager: TelegramManager, stop_event: asyncio.Event) -> None:
    """Same stop_event-driven shape as the old quarantine.purge_loop --
    runs until the app's shutdown event fires."""
    while not stop_event.is_set():
        if config.get("photos_backup_enabled"):
            last_checked = db.get_photos_backup_last_checked()
            interval_seconds = config.get("photos_backup_poll_interval_minutes") * 60
            if last_checked is None or time.time() - last_checked >= interval_seconds:
                try:
                    await poll_cycle(manager)
                except Exception:
                    logger.exception("Photos backup poll cycle failed")
                finally:
                    db.set_photos_backup_last_checked(time.time())
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_TICK_SECONDS)
        except asyncio.TimeoutError:
            pass

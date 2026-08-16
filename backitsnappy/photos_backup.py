"""Orchestrates the Automatic Photos Backup feature: poll the Photos
library for new items, export -> upload -> log, then delete everything
that uploaded successfully in one batch at the end of the cycle.

Deleting is batched, and separated from the per-item work, for a reason
that isn't obvious: it goes through PhotoKit (see
photos_automation.delete_items), and macOS shows the user one confirmation
prompt per change request. Batching a cycle's worth of items into a single
request means one prompt instead of one per photo.

Ordering matters as much as batching. An item is logged to
photos_backup_log the moment its upload is confirmed, *before* any delete
is attempted -- so "backed up so far" counts what's genuinely safe in
Telegram, and a delete that fails (or that the user declines at the
prompt) leaves the item in the library rather than un-counting a real
backup.

Upload completion is observed by polling the same job dict the frontend's
own progress bar polls (manager.jobs[job_id], see api/routes_upload.py) --
deliberately not client_manager's on_confirmed callback mechanism, since
that would need a sync-callback-schedules-async-task bridge for no real
benefit: this module already needs job["status"] and job["file_id"], both
already used elsewhere for exactly this. A file is only ever considered
"safely backed up" once job["status"] == "done" and a real files.id row is
confirmed present.
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

# Items currently mid-export/upload -- guards against a poll cycle
# re-discovering an item as "new" while it's still in flight, before its
# photos_backup_log row exists.
_in_flight: set[str] = set()

# Live progress for the Settings UI -- "is a poll cycle actively running
# right now, and how far through it." A plain module-level dict rather than
# anything durable: this is a cosmetic in-progress indicator, not something
# that needs to survive a restart.
_status = {"active": False, "total": 0, "done": 0}


def get_status() -> dict:
    return dict(_status)


async def _process_item(manager: TelegramManager, album_id: int, item_id: str, filename: str) -> str | None:
    """Exports and uploads one item, logging it as backed up once the
    upload is confirmed. Returns the item id if it is now safe to delete
    from Photos, or None if anything went wrong (in which case the item
    stays in the library). Deleting is deliberately *not* done here -- see
    poll_cycle, which batches it."""
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
                return None

            file_row = db.get_file(job["file_id"])
            if file_row is None:
                logger.warning(
                    "Photos backup: uploaded file row vanished for %s (%s) before delete",
                    filename, path.name,
                )
                return None
            file_rows.append(file_row)

        # Log every component as backed up the moment its upload is
        # confirmed -- deliberately *before* attempting the delete below,
        # not after. "Backed up so far" in Settings counts these rows, and
        # the delete step is a separate, currently-flaky operation (see
        # the -10000 AppleEvent issue): a file that's safely in Telegram
        # should count as backed up regardless of whether Photos.app lets
        # it be removed from the library yet.
        now = time.time()
        for file_row in file_rows:
            db.insert_photos_backup_log(
                photos_item_id=item_id,
                filename=file_row["filename"],
                size=file_row["size"],
                telegram_message_id=file_row["telegram_message_id"],
                channel_id=file_row["channel_id"],
                uploaded_at=now,
            )
        logger.info(
            "Photos backup: uploaded %d file(s) for %s (item %s)",
            len(file_rows), filename, item_id,
        )
        # Every exported component now has a real files.id row -- each only
        # ever inserted after Telegram actually returned a message id
        # (fresh, forwarded, or deduped-existing; see
        # client_manager._do_upload). Safe to delete, so hand the id back
        # for poll_cycle's batched delete.
        return item_id
    except Exception:
        logger.exception("Photos backup failed for item %s (%s)", item_id, filename)
        return None
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
    if new_items:
        logger.info("Photos backup: found %d new item(s)", len(new_items))
        album_id = await manager.ensure_photos_backup_album()
        _status.update(active=True, total=len(new_items), done=0)
        try:
            await asyncio.gather(
                *(_process_item(manager, album_id, item_id, filename) for item_id, filename in new_items)
            )
        finally:
            _status["active"] = False

    # Runs every cycle, including cycles that found nothing new to upload.
    # It sweeps *everything* still pending deletion, not just this cycle's
    # uploads: an item backed up today only becomes deletable once it's
    # older than the configured window, so the whole backlog has to be
    # re-examined each time. That's also what lets a photo backed up weeks
    # ago get cleared out on the cycle it finally ages past the threshold,
    # with no separate scheduling to maintain.
    await sweep_deletions()


async def pending_deletion_ids() -> list[str]:
    """Items confirmed backed up to Telegram that are still in the Photos
    library. These are safe to delete at any time -- the age window is a
    user preference about *when* to reclaim the space, not a safety
    property."""
    rows = db.get_connection().execute(
        "SELECT DISTINCT photos_item_id FROM photos_backup_log WHERE deleted_at IS NULL"
    ).fetchall()
    return [row["photos_item_id"] for row in rows]


async def eligible_for_deletion(item_ids: list[str]) -> list[str]:
    """Filters pending items down to those older than the configured
    window. Items whose id no longer resolves to a real asset are dropped
    too -- they've already left the library by some other route, so
    there's nothing to delete."""
    if not item_ids:
        return []
    days = config.get("photos_backup_delete_after_days")
    dates = await photos_automation.get_creation_dates(item_ids)
    if days <= 0:
        return [i for i in item_ids if i in dates]
    cutoff = time.time() - days * 86400
    return [i for i in item_ids if i in dates and dates[i] <= cutoff]


async def _delete_and_mark(item_ids: list[str], reason: str) -> int:
    """Shared tail of both the scheduled sweep and the manual "free up
    space now" action: one batched PhotoKit delete, then record the
    timestamp for whatever actually went."""
    if not item_ids:
        return 0
    try:
        deleted_count = await photos_automation.delete_items(item_ids)
    except photos_automation.PhotosAutomationError:
        logger.exception(
            "Photos backup: %d item(s) are uploaded but couldn't be deleted from Photos (%s)",
            len(item_ids), reason,
        )
        return 0
    if deleted_count:
        now = time.time()
        for item_id in item_ids:
            db.mark_photos_backup_deleted(item_id, now)
    logger.info(
        "Photos backup: deleted %d of %d backed-up item(s) from Photos (%s)",
        deleted_count, len(item_ids), reason,
    )
    return deleted_count


async def sweep_deletions() -> int:
    """Deletes every backed-up item that has aged past the configured
    window. Called at the end of each poll cycle."""
    pending = await pending_deletion_ids()
    eligible = await eligible_for_deletion(pending)
    if not eligible:
        if pending:
            logger.info(
                "Photos backup: %d item(s) backed up, none older than the %d-day delete window yet",
                len(pending), config.get("photos_backup_delete_after_days"),
            )
        return 0
    return await _delete_and_mark(eligible, "aged past the delete window")


async def delete_all_backed_up_now() -> int:
    """Manual "free up iCloud storage now" -- deletes every backed-up item
    still in the library, ignoring the age window entirely. Only ever
    triggered explicitly by the user from Settings; the age window exists
    to keep recent photos on their phone, and choosing this is choosing to
    override that for the sake of space."""
    pending = await pending_deletion_ids()
    if not pending:
        return 0
    # Still resolve through PhotoKit first so ids that already left the
    # library by some other route don't count toward the result.
    dates = await photos_automation.get_creation_dates(pending)
    present = [i for i in pending if i in dates]
    return await _delete_and_mark(present, "manual free-up-space request")


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

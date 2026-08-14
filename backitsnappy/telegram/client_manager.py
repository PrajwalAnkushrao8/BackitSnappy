"""Owns the Telethon client for the app's lifetime: auth flow, storage/album
channel management, deduped uploads with progress tracking, and album invites.

All public async methods must run on the same asyncio event loop the client
was created on (the app's background loop) — see main.py. `queue_upload_threadsafe`
is the one exception, safe to call from other threads (the folder watcher).
"""
import asyncio
import hashlib
import logging
import mimetypes
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

from telethon import TelegramClient, functions
from telethon.errors import (
    ChannelPrivateError,
    ChannelTooLargeError,
    FloodWaitError,
    MessageDeleteForbiddenError,
    MessageIdInvalidError,
    PeerFloodError,
    RPCError,
    SessionPasswordNeededError,
    UserPrivacyRestrictedError,
)
from telethon.sessions import StringSession

from .. import config, db, keepawake, media, secrets_store
from .auth_flow import AuthState

logger = logging.getLogger(__name__)

STORAGE_CHANNEL_TITLE = "BackitSnappy Storage"
IPHONE_BACKUP_ALBUM_NAME = "iPhone Backup"
DEFAULT_UPLOAD_CAP_BYTES = 2 * 1024 * 1024 * 1024
PREMIUM_UPLOAD_CAP_BYTES = 4 * 1024 * 1024 * 1024

# A bare concurrency cap isn't enough to stay under Telegram's flood limits --
# large files self-throttle by taking a while, but a burst of many small
# files under a concurrency cap alone can still fire requests fast enough to
# trip it. Pacing bounds request *rate*, the semaphore bounds *concurrency*.
UPLOAD_CONCURRENCY = 2
UPLOAD_PACING_SECONDS = 0.7

# Hashing and thumbnailing (ffmpeg for videos) are CPU/disk-bound, not
# network-bound -- unlike the upload step above, Telegram never sees this
# work, so it isn't flood-wait-sensitive. But it also wasn't bounded at all:
# a batch of 25 files uploading at once meant up to 25 concurrent ffmpeg
# subprocesses fighting for CPU, which starved the 2 uploads that were
# actually allowed to run. Cap it separately, well under core count so the
# event loop and the in-flight uploads still get scheduled promptly.
PREP_CONCURRENCY = 4

# Sync's import side downloads each not-yet-indexed message sequentially
# (GetFileRequest is a separate flood-wait bucket from uploads, but still
# worth a light pacing delay for anyone syncing a channel with many new
# manually-added files at once).
IMPORT_PACING_SECONDS = 0.3

# Auto-pause-and-resume for a FloodWaitError this many times (sleeping the
# exact reported wait each time) before finally giving up and surfacing an
# error -- bounds a pathological repeatedly-flooding account without
# treating an ordinary rate limit as a hard failure.
FLOOD_WAIT_MAX_RETRIES = 5


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _unique_path(path: Path) -> Path:
    """Finder-style collision handling: foo.jpg -> foo (1).jpg -> foo (2).jpg."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    counter = 1
    while True:
        candidate = path.with_name(f"{stem} ({counter}){suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


class TelegramManager:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.client: TelegramClient | None = None
        self.state: AuthState = AuthState.NEEDS_CREDENTIALS
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        self.upload_cap_bytes: int = DEFAULT_UPLOAD_CAP_BYTES
        self.jobs: dict[str, dict] = {}
        self._upload_semaphore = asyncio.Semaphore(UPLOAD_CONCURRENCY)
        self._prep_semaphore = asyncio.Semaphore(PREP_CONCURRENCY)
        # Per-(hash, album) locks close the TOCTOU race where two uploads of
        # identical content both pass the dedup check before either has
        # inserted its row -- see _do_upload. Safe to grow/read without its
        # own lock: the check-and-create below has no `await` in between, so
        # it's atomic under asyncio's cooperative scheduling.
        self._upload_locks: dict[tuple[str, int | None], asyncio.Lock] = {}
        # Lets a still-running download be cancelled from the API -- keyed by
        # the same job_id the frontend polls for progress.
        self._download_tasks: dict[str, asyncio.Task] = {}
        # Auto-sync once an "api"-sourced (phone) upload batch settles --
        # same debounce shape as keepawake's stop timer (and reuses its
        # grace period) since a batch is a sequence of independent requests
        # with real gaps between them, not one atomic operation.
        self._api_batch_active_count = 0
        self._api_batch_sync_task: asyncio.Task | None = None

    # --- lifecycle -----------------------------------------------------

    async def start(self) -> AuthState:
        """Called once at app startup: try to resume an existing session
        without prompting the user."""
        creds = secrets_store.get_api_credentials()
        if not creds:
            self.state = AuthState.NEEDS_CREDENTIALS
            return self.state

        api_id, api_hash = creds
        session_str = secrets_store.get_session_string() or ""
        self.client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await self.client.connect()

        if session_str and await self.client.is_user_authorized():
            self.state = AuthState.AUTHORIZED
            await self._post_auth_setup()
        else:
            self.state = AuthState.NEEDS_PHONE
        return self.state

    async def disconnect(self) -> None:
        if self.client:
            await self.client.disconnect()

    # --- auth flow -------------------------------------------------------

    async def set_credentials(self, api_id: int, api_hash: str) -> AuthState:
        secrets_store.set_api_credentials(api_id, api_hash)
        self.client = TelegramClient(StringSession(), api_id, api_hash)
        await self.client.connect()
        self.state = AuthState.NEEDS_PHONE
        return self.state

    async def send_code(self, phone: str) -> AuthState:
        if self.client is None:
            raise RuntimeError("Telegram API credentials not set yet")
        sent = await self.client.send_code_request(phone)
        self._phone = phone
        self._phone_code_hash = sent.phone_code_hash
        self.state = AuthState.NEEDS_CODE
        return self.state

    async def submit_code(self, code: str) -> AuthState:
        try:
            await self.client.sign_in(
                self._phone, code, phone_code_hash=self._phone_code_hash
            )
        except SessionPasswordNeededError:
            self.state = AuthState.NEEDS_PASSWORD
            return self.state
        await self._finish_login()
        return self.state

    async def submit_password(self, password: str) -> AuthState:
        await self.client.sign_in(password=password)
        await self._finish_login()
        return self.state

    async def _finish_login(self) -> None:
        secrets_store.set_session_string(self.client.session.save())
        self.state = AuthState.AUTHORIZED
        await self._post_auth_setup()

    async def logout(self) -> AuthState:
        """Fully signs out: disconnects, clears the saved session, and
        wipes the local index -- so a different account signing in next
        doesn't see stale references to channels it can't access. Keeps
        api_id/api_hash (this app's own Telegram credentials, not the
        account's) so re-signing-in skips straight to the phone-number
        step. Nothing in Telegram itself is touched; logging back into an
        account that had BackitSnappy content rebuilds the local index
        automatically via _post_auth_setup's discovery step."""
        if self.client:
            await self.client.disconnect()
        secrets_store.clear_session()
        db.wipe_local_index()
        config.set("storage_channel_id", None)
        config.set("iphone_backup_album_id", None)
        self.jobs.clear()

        api_id, api_hash = secrets_store.get_api_credentials()
        self.client = TelegramClient(StringSession(), api_id, api_hash)
        await self.client.connect()
        self.state = AuthState.NEEDS_PHONE
        return self.state

    async def _post_auth_setup(self) -> None:
        # StringSession doesn't persist entity access_hashes across restarts,
        # so re-fetch dialogs each start to warm the in-memory cache before
        # resolving any previously-stored channel IDs.
        try:
            await self.client.get_dialogs()
        except Exception:
            logger.exception("Failed to warm entity cache via get_dialogs")
        await self._refresh_upload_cap()
        if config.get("storage_channel_id") is None:
            # No local record of a storage channel -- either a genuinely
            # new account, or this account's local index was wiped (e.g.
            # by logout()). Scan for channels this account already owns
            # that BackitSnappy previously created and rebuild the local
            # index from them before falling back to creating fresh ones,
            # so logging back into an account that had content gets it all
            # back automatically.
            await self._discover_existing_channels()
            await self.sync_all()
        await self.ensure_storage_channel()
        await self.ensure_iphone_backup_album()

    async def _discover_existing_channels(self) -> None:
        """Scans this account's own broadcast channels for ones BackitSnappy
        created previously (identified by the exact `about` text set at
        creation time -- title alone isn't reliable since albums are
        user-named), and re-registers them locally. Only the channels this
        account itself created are even fetched in full, to keep this a
        cheap one-time scan rather than probing every dialog."""
        async for dialog in self.client.iter_dialogs():
            entity = dialog.entity
            if not (getattr(entity, "broadcast", False) and getattr(entity, "creator", False)):
                continue
            try:
                full = await self.client(functions.channels.GetFullChannelRequest(entity))
            except Exception:
                logger.exception("Failed to inspect channel %s during discovery", entity.id)
                continue
            about = (full.full_chat.about or "").strip()
            if about == "BackitSnappy personal file storage":
                config.set("storage_channel_id", entity.id)
            elif about == "BackitSnappy shared album" and db.get_album_by_channel(entity.id) is None:
                db.insert_album(entity.title, entity.id)

    async def _refresh_upload_cap(self) -> None:
        me = await self.client.get_me()
        self.upload_cap_bytes = (
            PREMIUM_UPLOAD_CAP_BYTES if getattr(me, "premium", False) else DEFAULT_UPLOAD_CAP_BYTES
        )

    # --- storage channel -------------------------------------------------

    async def ensure_storage_channel(self) -> int:
        channel_id = config.get("storage_channel_id")
        if channel_id:
            try:
                await self.client.get_entity(channel_id)
                return channel_id
            except (ValueError, TypeError):
                logger.warning("Stored storage_channel_id unresolvable, recreating")

        result = await self.client(
            functions.channels.CreateChannelRequest(
                title=STORAGE_CHANNEL_TITLE,
                about="BackitSnappy personal file storage",
                broadcast=True,
                megagroup=False,
            )
        )
        channel = result.chats[0]
        config.set("storage_channel_id", channel.id)
        return channel.id

    # --- iPhone backup album ----------------------------------------------

    async def ensure_iphone_backup_album(self) -> int:
        """Create the fixed "iPhone Backup" album once -- the iOS Shortcuts
        upload flow needs a stable album_id to target. Idempotent, same
        pattern as ensure_storage_channel: reused on every later launch,
        only recreated if the stored one was deleted."""
        album_id = config.get("iphone_backup_album_id")
        if album_id is not None and db.get_album(album_id) is not None:
            return album_id
        result = await self.create_album(IPHONE_BACKUP_ALBUM_NAME)
        config.set("iphone_backup_album_id", result["id"])
        return result["id"]

    # --- uploads -----------------------------------------------------------

    def start_upload(
        self, path: str | Path, album_id: int | None = None, source: str = "manual", delete_after: bool = False,
        queue_id: int | None = None,
    ) -> str:
        """Schedule an upload from within the manager's own event loop
        (e.g. a FastAPI route handler) and return a job_id immediately.
        queue_id, if given, is a durable upload_queue row (see db.py) that
        gets removed once this upload reaches a terminal state -- rows
        still present on the next app launch are resumed from disk."""
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"percent": 0, "status": "queued", "filename": Path(path).name}
        asyncio.create_task(self._do_upload(job_id, Path(path), album_id, source, delete_after, queue_id))
        return job_id

    def queue_upload_threadsafe(
        self, path: str | Path, album_id: int | None = None, source: str = "watcher", delete_after: bool = False,
        queue_id: int | None = None,
    ) -> str:
        """Thread-safe variant for callers outside the event loop (folder watcher)."""
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"percent": 0, "status": "queued", "filename": Path(path).name}
        asyncio.run_coroutine_threadsafe(
            self._do_upload(job_id, Path(path), album_id, source, delete_after, queue_id), self._loop
        )
        return job_id

    def _get_upload_lock(self, key: tuple[str, int | None]) -> asyncio.Lock:
        lock = self._upload_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._upload_locks[key] = lock
        return lock

    def _insert_file_safely(self, sha256_hash: str, album_id: int | None, **kwargs) -> int:
        """Defense-in-depth against the same TOCTOU race the per-(hash, album)
        lock in _do_upload already prevents in-process -- if some other path
        (e.g. the folder watcher racing the API) still raced us to the
        INSERT, treat the lost UNIQUE-constraint race as a successful dedup
        instead of crashing the job with a raw DB error."""
        try:
            return db.insert_file(sha256_hash=sha256_hash, album_id=album_id, **kwargs)
        except sqlite3.IntegrityError:
            existing = db.get_file_by_hash_and_album(sha256_hash, album_id)
            if existing:
                return existing["id"]
            raise

    def _api_upload_batch_started(self) -> None:
        self._api_batch_active_count += 1
        if self._api_batch_sync_task is not None:
            self._api_batch_sync_task.cancel()
            self._api_batch_sync_task = None

    def _api_upload_batch_finished(self) -> None:
        self._api_batch_active_count = max(0, self._api_batch_active_count - 1)
        if self._api_batch_active_count > 0:
            return
        if self._api_batch_sync_task is None:
            self._api_batch_sync_task = asyncio.create_task(self._sync_after_api_batch())

    async def _sync_after_api_batch(self) -> None:
        try:
            await asyncio.sleep(keepawake.STOP_GRACE_SECONDS)
        except asyncio.CancelledError:
            return
        self._api_batch_sync_task = None
        if self._api_batch_active_count > 0:
            return
        logger.info("Auto-syncing with Telegram after a phone upload batch settled")
        try:
            await self.sync_all()
        except Exception:
            logger.exception("Auto-sync after phone upload batch failed")

    async def _do_upload(
        self, job_id: str, path: Path, album_id: int | None, source: str, delete_after: bool = False,
        queue_id: int | None = None,
    ) -> None:
        job = self.jobs[job_id]
        try:
            if source == "api":
                await keepawake.upload_started()
                self._api_upload_batch_started()
            size = path.stat().st_size
            if size > self.upload_cap_bytes:
                cap_gb = self.upload_cap_bytes // (1024**3)
                raise ValueError(f"File exceeds the {cap_gb}GB limit for this Telegram account")

            async with self._prep_semaphore:
                job["status"] = "hashing"
                sha256_hash = await asyncio.to_thread(_hash_file, path)
                mime_type = mimetypes.guess_type(path.name)[0]

                try:
                    await asyncio.to_thread(media.generate_thumbnail, path, sha256_hash, mime_type)
                except Exception:
                    logger.exception("Thumbnail generation crashed for %s", path)

            # Held across check -> upload/forward -> insert so a second
            # concurrent upload of identical content (e.g. Save-As
            # duplicates dropped in the same batch, or the same file arriving
            # via both the persistent-queue resume and a fresh re-drop) waits
            # for the first to finish and then correctly dedupes against it,
            # instead of both racing Telegram and the DB. Update the status
            # before a possibly-long wait so the UI doesn't keep showing a
            # stale "hashing" label while this is actually just queued behind
            # an identical in-flight upload.
            job["status"] = "waiting"
            async with self._get_upload_lock((sha256_hash, album_id)):
                existing_here = db.get_file_by_hash_and_album(sha256_hash, album_id)
                if existing_here:
                    job.update(percent=100, status="done", file_id=existing_here["id"], deduped=True)
                    return

                storage_copy = db.get_storage_copy_by_hash(sha256_hash)
                if storage_copy and album_id is not None:
                    job["status"] = "forwarding"
                    file_id = await self._forward_into_album(sha256_hash, storage_copy, album_id, source)
                    job.update(percent=100, status="done", file_id=file_id, deduped=True)
                    return

                job["status"] = "uploading"
                file_id = await self._upload_fresh(job_id, path, size, sha256_hash, mime_type, album_id, source)
                job.update(percent=100, status="done", file_id=file_id, deduped=False)
        except Exception as exc:
            logger.exception("Upload failed for %s", path)
            job.update(status="error", error=str(exc))
        finally:
            if source == "api":
                await keepawake.upload_finished()
                self._api_upload_batch_finished()
            if delete_after:
                path.unlink(missing_ok=True)
            if queue_id is not None:
                # Reached a terminal state (success or error) in this
                # session -- no longer needs to survive a restart. A row
                # only lingers in upload_queue if the process ends before
                # this point, which is exactly the crash/close case that's
                # meant to be resumed on next launch.
                db.dequeue_upload(queue_id)

    async def _forward_into_album(self, sha256_hash: str, storage_copy, album_id: int, source: str) -> int:
        album = db.get_album(album_id)
        storage_entity = await self.client.get_entity(config.get("storage_channel_id"))
        album_entity = await self.client.get_entity(album["telegram_channel_id"])
        [forwarded] = await self.client.forward_messages(
            album_entity, [storage_copy["telegram_message_id"]], from_peer=storage_entity
        )
        return self._insert_file_safely(
            sha256_hash=sha256_hash,
            album_id=album_id,
            filename=storage_copy["filename"],
            size=storage_copy["size"],
            mime_type=storage_copy["mime_type"],
            telegram_message_id=forwarded.id,
            channel_id=album["telegram_channel_id"],
            source=source,
        )

    async def _upload_fresh(
        self, job_id: str, path: Path, size: int, sha256_hash: str, mime_type: str | None,
        album_id: int | None, source: str,
    ) -> int:
        if album_id is not None:
            target_channel_id = db.get_album(album_id)["telegram_channel_id"]
        else:
            target_channel_id = config.get("storage_channel_id")
        entity = await self.client.get_entity(target_channel_id)
        job = self.jobs[job_id]

        def _progress(current: int, total: int) -> None:
            job["percent"] = int(current * 100 / total) if total else 0

        async with self._upload_semaphore:
            # Telethon already auto-retries internally for waits under its
            # own flood_sleep_threshold (60s, unset by us); only longer
            # waits surface here. Auto-pause-and-resume for the *exact*
            # reported wait, up to FLOOD_WAIT_MAX_RETRIES times, rather than
            # failing the upload outright on the first one -- for a
            # single-account personal app there's no abuse risk in waiting
            # out a legitimate rate limit. The cap exists only so a
            # genuinely broken/restricted account doesn't hang a job
            # forever; a real error still surfaces after that many misses.
            attempt = 0
            while True:
                try:
                    message = await self.client.send_file(entity, str(path), progress_callback=_progress)
                    break
                except FloodWaitError as exc:
                    attempt += 1
                    if attempt > FLOOD_WAIT_MAX_RETRIES:
                        raise
                    logger.warning(
                        "Flood wait of %s seconds for %s (attempt %s/%s)",
                        exc.seconds, path, attempt, FLOOD_WAIT_MAX_RETRIES,
                    )
                    job["status"] = f"rate limited by Telegram, retrying in {exc.seconds}s"
                    await asyncio.sleep(exc.seconds)
                    job["status"] = "uploading"
            # Bounds request *rate*, not just concurrency -- a burst of many
            # small files can still trip Telegram's flood limit under a bare
            # concurrency cap, since each completes too fast for it to help.
            await asyncio.sleep(UPLOAD_PACING_SECONDS)

        return self._insert_file_safely(
            sha256_hash=sha256_hash,
            album_id=album_id,
            filename=path.name,
            size=size,
            mime_type=mime_type,
            telegram_message_id=message.id,
            channel_id=target_channel_id,
            source=source,
        )

    # --- albums ------------------------------------------------------------

    async def create_album(self, name: str) -> dict:
        result = await self.client(
            functions.channels.CreateChannelRequest(
                title=name, about="BackitSnappy shared album", broadcast=True, megagroup=False
            )
        )
        channel = result.chats[0]
        album_id = db.insert_album(name, channel.id)
        return {"id": album_id, "name": name, "telegram_channel_id": channel.id}

    async def invite_to_album(self, album_id: int, username: str) -> dict:
        album = db.get_album(album_id)
        if not album:
            raise ValueError("Album not found")
        username = username.lstrip("@")
        channel_entity = await self.client.get_entity(album["telegram_channel_id"])
        try:
            user_entity = await self.client.get_input_entity(username)
            await self.client(functions.channels.InviteToChannelRequest(channel_entity, [user_entity]))
            db.insert_album_member(album_id, username)
            return {"method": "direct", "invite_link": None}
        except (UserPrivacyRestrictedError, PeerFloodError, ValueError) as exc:
            logger.info("Direct invite failed for %s (%s), falling back to invite link", username, exc)
            link = await self.get_invite_link(album_id)
            db.insert_album_member(album_id, username)
            return {"method": "link", "invite_link": link}

    async def get_invite_link(self, album_id: int) -> str:
        album = db.get_album(album_id)
        channel_entity = await self.client.get_entity(album["telegram_channel_id"])
        result = await self.client(functions.messages.ExportChatInviteRequest(channel_entity))
        return result.link

    # --- sync -----------------------------------------------------------

    async def sync_channel(self, channel_id: int, album_id: int | None) -> dict:
        """Reconcile the local index against a Telegram channel in both
        directions: remove rows for messages deleted directly in Telegram
        (outside the app, which we'd otherwise have no way to know about),
        and import rows for messages that exist in Telegram but aren't
        indexed yet (e.g. a photo added manually via the Telegram app)."""
        try:
            entity = await self.client.get_entity(channel_id)
        except (ValueError, TypeError):
            # The whole channel is gone, not just some messages in it --
            # every local row referencing it is stale, and if it was an
            # album, the album itself no longer exists either.
            rows = db.get_files_by_channel(channel_id)
            for row in rows:
                db.delete_file(row["id"])
            if album_id is not None:
                db.delete_album(album_id)
            return {"removed": len(rows), "added": 0}

        tracked_message_ids = {row["telegram_message_id"] for row in db.get_files_by_channel(channel_id)}
        live_messages = [msg async for msg in self.client.iter_messages(entity)]
        live_message_ids = {msg.id for msg in live_messages}

        removed = 0
        for row in db.get_files_by_channel(channel_id):
            if row["telegram_message_id"] not in live_message_ids:
                db.delete_file(row["id"])
                removed += 1

        added = 0
        for msg in live_messages:
            if msg.id in tracked_message_ids or not msg.file:
                continue
            try:
                if await self._import_message(msg, channel_id, album_id):
                    added += 1
                await asyncio.sleep(IMPORT_PACING_SECONDS)
            except Exception:
                logger.exception("Failed to import message %s from channel %s", msg.id, channel_id)

        return {"removed": removed, "added": added}

    async def _import_message(self, msg, channel_id: int, album_id: int | None) -> bool:
        """Pull a Telegram-only message into the local index: download it
        (needed to compute a real sha256 hash, so it dedupes consistently
        with app-uploaded copies of the same content), generate a
        thumbnail, and insert the row. Returns whether it was imported
        (False if it turned out to already be a known duplicate)."""
        filename = msg.file.name or f"telegram_{msg.id}{mimetypes.guess_extension(msg.file.mime_type or '') or ''}"
        tmp_target = Path(tempfile.gettempdir()) / f"backitsnappy-sync-{channel_id}-{msg.id}"
        # download_media may append its own extension to the given path (it
        # picks one based on the media's mime type) -- its return value is
        # the actual path written, which is what must be used from here on.
        tmp_path = Path(await self.client.download_media(msg, file=str(tmp_target)))
        try:
            sha256_hash = await asyncio.to_thread(_hash_file, tmp_path)

            if db.get_file_by_hash_and_album(sha256_hash, album_id):
                return False  # already tracked, somehow, under a different message id

            mime_type = msg.file.mime_type
            cache_path = media.cached_media_path(sha256_hash, filename)
            if not cache_path.exists():
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(tmp_path, cache_path)

            await self._ensure_thumbnail_fast(msg, cache_path, sha256_hash, mime_type)

            self._insert_file_safely(
                sha256_hash=sha256_hash,
                album_id=album_id,
                filename=filename,
                size=msg.file.size,
                mime_type=mime_type,
                telegram_message_id=msg.id,
                channel_id=channel_id,
                source="telegram",
            )
            return True
        finally:
            tmp_path.unlink(missing_ok=True)

    async def _ensure_thumbnail_fast(
        self, msg, cache_path: Path, sha256_hash: str, mime_type: str | None
    ) -> None:
        """Prefer Telegram's own pre-generated low-res preview (a tiny,
        separate download, `thumb=-1` picks the best available one) over
        regenerating one locally via Pillow/ffmpeg on the full file --
        much cheaper per file, especially for videos where ffmpeg has real
        subprocess overhead. Falls back to local generation if Telegram
        doesn't have one for this message (some file types don't)."""
        dest = media.thumbnail_path(sha256_hash)
        if dest.exists():
            return
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            thumb_tmp_target = dest.parent / f"{sha256_hash}.thumbpart"
            # Same "download_media may pick its own extension" caveat as
            # the main download above -- use the returned path, not the
            # one requested.
            downloaded = await self.client.download_media(msg, thumb=-1, file=str(thumb_tmp_target))
            if downloaded:
                Path(downloaded).replace(dest)
                return
        except Exception:
            logger.exception(
                "Fast thumbnail download failed for message %s, falling back to local generation", msg.id
            )
        try:
            await asyncio.to_thread(media.generate_thumbnail, cache_path, sha256_hash, mime_type)
        except Exception:
            logger.exception("Local thumbnail generation crashed for imported message %s", msg.id)

    async def sync_all(self) -> dict:
        """Sync every known channel: the storage channel and every album."""
        channel_targets: list[tuple[int, int | None]] = []
        storage_channel_id = config.get("storage_channel_id")
        if storage_channel_id:
            channel_targets.append((storage_channel_id, None))
        for album in db.list_albums():
            channel_targets.append((album["telegram_channel_id"], album["id"]))

        total_removed = 0
        total_added = 0
        for channel_id, album_id in channel_targets:
            try:
                result = await self.sync_channel(channel_id, album_id)
                total_removed += result["removed"]
                total_added += result["added"]
            except Exception:
                logger.exception("Sync failed for channel %s", channel_id)
        return {"channels_checked": len(channel_targets), "removed": total_removed, "added": total_added}

    async def delete_file(self, file_id: int) -> None:
        """Permanently delete one file's message from its Telegram channel,
        then drop its index row. Telegram-side deletion happens first, so a
        failure there leaves the row untouched and the operation retryable
        rather than orphaning a DB row Telegram no longer has."""
        file_row = db.get_file(file_id)
        if file_row is None:
            raise ValueError("File not found")
        try:
            entity = await self.client.get_entity(file_row["channel_id"])
            await self.client.delete_messages(entity, [file_row["telegram_message_id"]])
        except (MessageIdInvalidError, MessageDeleteForbiddenError, ChannelPrivateError) as exc:
            raise ValueError(f"Could not delete file from Telegram: {exc}") from exc
        except RPCError as exc:
            raise ValueError(f"Telegram error while deleting file: {exc}") from exc
        db.delete_file(file_id)

    async def delete_album(self, album_id: int) -> None:
        """Permanently destroy an album's Telegram channel (not just leave
        it -- client.delete_dialog() would only leave, silently keeping the
        channel and its contents intact for everyone else) and drop its
        index rows. This account is always the channel's creator (it
        created every album channel itself), so no permission pre-check is
        needed; DeleteChannelRequest is a single atomic RPC."""
        album = db.get_album(album_id)
        if album is None:
            raise ValueError("Album not found")
        try:
            entity = await self.client.get_entity(album["telegram_channel_id"])
            await self.client(functions.channels.DeleteChannelRequest(entity))
        except ChannelTooLargeError as exc:
            raise ValueError("Album channel has too many members to delete") from exc
        except ChannelPrivateError:
            # Telegram reports the channel as invalid/inaccessible -- this is
            # exactly what a channel that's already gone looks like (a
            # retried delete, or a first attempt whose success response got
            # lost to a network blip while the deletion itself went through
            # server-side). There's nothing left to delete on Telegram's
            # side either way, so finish the local cleanup instead of
            # raising -- otherwise the index is left pointing at a channel
            # that's already gone, and the user sees an error for an
            # operation that, from their perspective, actually worked.
            logger.info(
                "Album %s's channel already gone from Telegram (ChannelPrivateError) -- "
                "treating as already deleted, cleaning up local index", album_id,
            )
        except RPCError as exc:
            raise ValueError(f"Telegram error while deleting album: {exc}") from exc
        db.delete_album(album_id)

    # --- media cache / downloads --------------------------------------------

    async def ensure_local_media(self, file_row, job_id: str | None = None) -> Path:
        """Return a fully-downloaded local copy of this file, downloading
        from Telegram into the media cache first if not already cached."""
        dest = media.cached_media_path(file_row["sha256_hash"], file_row["filename"])
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)

        entity = await self.client.get_entity(file_row["channel_id"])
        message = await self.client.get_messages(entity, ids=file_row["telegram_message_id"])
        if message is None:
            raise ValueError("Original Telegram message no longer exists")

        def _progress(current: int, total: int) -> None:
            if job_id and job_id in self.jobs:
                self.jobs[job_id]["percent"] = int(current * 100 / total) if total else 0

        tmp_dest = dest.with_name(dest.name + ".part")
        await self.client.download_media(message, file=str(tmp_dest), progress_callback=_progress)
        tmp_dest.rename(dest)
        return dest

    def start_prepare(self, file_id: int) -> str:
        """Ensure a file is downloaded to the local media cache (for lightbox
        viewing) and return a job_id to poll for progress."""
        file_row = db.get_file(file_id)
        if file_row is None:
            raise ValueError("File not found")
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"percent": 0, "status": "queued", "filename": file_row["filename"]}
        asyncio.create_task(self._do_prepare(job_id, file_row))
        return job_id

    async def _do_prepare(self, job_id: str, file_row) -> None:
        job = self.jobs[job_id]
        try:
            job["status"] = "downloading"
            await self.ensure_local_media(file_row, job_id=job_id)
            job.update(percent=100, status="done")
        except Exception as exc:
            logger.exception("Prepare failed for file %s", file_row["id"])
            job.update(status="error", error=str(exc))

    def start_download(
        self, file_id: int, destination_type: str, destination_path: str | None = None
    ) -> str:
        """Download a file (via the media cache) to disk: either the default
        ~/Downloads/BackitSnappy/ folder (Finder-style collision suffixing)
        or an exact path the user chose via the native save panel."""
        file_row = db.get_file(file_id)
        if file_row is None:
            raise ValueError("File not found")
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"percent": 0, "status": "queued", "filename": file_row["filename"]}
        task = asyncio.create_task(
            self._do_download(job_id, file_row, destination_type, destination_path)
        )
        self._download_tasks[job_id] = task
        return job_id

    def cancel_download(self, job_id: str) -> bool:
        """Request cancellation of a still-running download job. Returns
        False if the job is unknown or already finished (nothing to cancel)."""
        task = self._download_tasks.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        return True

    async def _do_download(
        self, job_id: str, file_row, destination_type: str, destination_path: str | None
    ) -> None:
        job = self.jobs[job_id]
        try:
            job["status"] = "downloading"
            cached = await self.ensure_local_media(file_row, job_id=job_id)

            if destination_type == "custom":
                if not destination_path:
                    raise ValueError("No destination path given")
                dest = Path(destination_path)
            else:
                downloads_dir = Path.home() / "Downloads" / "BackitSnappy"
                downloads_dir.mkdir(parents=True, exist_ok=True)
                dest = _unique_path(downloads_dir / file_row["filename"])

            job["status"] = "saving"
            dest.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(shutil.copy2, cached, dest)
            job.update(percent=100, status="done", path=str(dest))
        except asyncio.CancelledError:
            job.update(status="error", error="Cancelled")
            cache_dest = media.cached_media_path(file_row["sha256_hash"], file_row["filename"])
            cache_dest.with_name(cache_dest.name + ".part").unlink(missing_ok=True)
            raise
        except Exception as exc:
            logger.exception("Download failed for file %s", file_row["id"])
            job.update(status="error", error=str(exc))
        finally:
            self._download_tasks.pop(job_id, None)

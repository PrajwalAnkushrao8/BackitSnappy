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
import uuid
from pathlib import Path
from typing import Callable

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
PHOTOS_BACKUP_ALBUM_NAME = "Photos Backup"
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

# Sync's import side downloads each not-yet-indexed message to hash it --
# GetFileRequest is a separate flood-wait bucket from uploads, so a handful
# run concurrently (same bounded-concurrency shape as UPLOAD_CONCURRENCY)
# rather than one at a time, with a per-import pacing delay still applied
# to each so the effective request rate stays gentle even at that
# concurrency -- matters most right after a fresh login, where the whole
# local index has to be rebuilt from scratch in one pass.
IMPORT_CONCURRENCY = 3
IMPORT_PACING_SECONDS = 0.3

# Auto-pause-and-resume for a FloodWaitError this many times (sleeping the
# exact reported wait each time) before finally giving up and surfacing an
# error -- bounds a pathological repeatedly-flooding account without
# treating an ordinary rate limit as a hard failure.
FLOOD_WAIT_MAX_RETRIES = 5

# One-time thumbnail backfill (see backfill_video_thumbnails): how much of
# a video's start/end gets fetched to build a thumbnail without
# downloading the whole file -- see media.generate_video_thumbnail_from_head_tail.
# Tail sizes escalate because the moov (index) atom's size scales with the
# video's length/track count, and there's no way to know it in advance
# without already having found it; most real files need only the first,
# smallest size, so this only costs extra requests for the few that don't.
THUMBNAIL_BACKFILL_HEAD_BYTES = 8 * 1024 * 1024
THUMBNAIL_BACKFILL_TAIL_SIZES = (2 * 1024 * 1024, 8 * 1024 * 1024, 24 * 1024 * 1024)
# Deliberately gentle and fully sequential -- this is a low-priority,
# one-time sweep over every already-synced video (hundreds, potentially),
# not something a user is waiting on, and it must never compete with an
# actively-open video's own streaming requests for Telegram's attention.
THUMBNAIL_BACKFILL_PACING_SECONDS = 1.0
THUMBNAIL_BACKFILL_FETCH_MAX_ATTEMPTS = 4
THUMBNAIL_BACKFILL_FETCH_RETRY_DELAY_SECONDS = 0.6

# Dedup fingerprint = sha256(first PARTIAL_HASH_SAMPLE_BYTES + last
# PARTIAL_HASH_SAMPLE_BYTES + exact byte size) rather than the whole file.
# Two different real-world media files matching on all three is not a
# realistic risk, and it means a Telegram-only file's fingerprint (see
# TelegramManager._hash_remote_file) can be computed from two small range
# downloads instead of pulling the entire file just to dedupe it -- the
# whole reason sync/import used to be so slow. Both this local version and
# the remote one MUST stay byte-for-byte consistent (same sample size, same
# head/tail/size order) or fresh uploads and synced-in files stop
# recognizing each other's duplicates.
PARTIAL_HASH_SAMPLE_BYTES = 64 * 1024


def _hash_file(path: Path) -> str:
    size = path.stat().st_size
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(PARTIAL_HASH_SAMPLE_BYTES))
        if size > PARTIAL_HASH_SAMPLE_BYTES:
            f.seek(size - PARTIAL_HASH_SAMPLE_BYTES)
            h.update(f.read(PARTIAL_HASH_SAMPLE_BYTES))
    h.update(str(size).encode())
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


# Locations under the home folder that macOS (or an app) will auto-execute
# or auto-trust the contents of -- a "Save As" target has no legitimate
# reason to be any of these, and writing into one turns a file save into
# persistence or credential theft.
_BLOCKED_SAVE_PREFIXES = (
    "Library/LaunchAgents",
    "Library/LaunchDaemons",
    "Library/StartupItems",
    "Library/Application Scripts",
    "Library/Services",
    "Library/Preferences",
    "Library/Keychains",
    ".ssh",
    ".aws",
    ".config",
)


def _validated_save_path(destination_path: str) -> Path:
    """Validates a "download to a custom location" target.

    The frontend sources this from the native save panel, but the HTTP API
    will accept any absolute path from any caller holding the pairing
    token -- so the constraint has to be enforced here rather than assumed
    from the caller. Confines saves to the user's own home folder and
    refuses the auto-run/credential directories inside it."""
    resolved = Path(destination_path).expanduser().resolve()
    home = Path.home().resolve()
    if not resolved.is_relative_to(home):
        raise ValueError("Destination must be inside your home folder")
    relative = str(resolved.relative_to(home))
    if any(relative == p or relative.startswith(p + "/") for p in _BLOCKED_SAVE_PREFIXES):
        raise ValueError("That location isn't allowed as a save destination")
    return resolved


def _phone_key(phone: str) -> str:
    """Digits-only normalization used purely as the db.api_credentials /
    last_authorized_phone lookup key, so "+1 555-123-4567" and
    "15551234567" bind to the same entry regardless of how it was typed.
    The original, user-typed string is still what's sent to Telegram's own
    APIs (send_code_request/sign_in already do their own phone parsing
    internally) -- this never touches that."""
    return "".join(ch for ch in phone if ch.isdigit())


class TelegramManager:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.client: TelegramClient | None = None
        self.state: AuthState = AuthState.NEEDS_PHONE
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        # Set by send_code when a phone number turns out to have no
        # api_id/api_hash bound yet (see db.api_credentials) -- holds it
        # until set_credentials binds one and requests the code. None the
        # rest of the time, including once login completes.
        self._pending_phone: str | None = None
        self.upload_cap_bytes: int = DEFAULT_UPLOAD_CAP_BYTES
        self.jobs: dict[str, dict] = {}
        self._upload_semaphore = asyncio.Semaphore(UPLOAD_CONCURRENCY)
        self._prep_semaphore = asyncio.Semaphore(PREP_CONCURRENCY)
        self._import_semaphore = asyncio.Semaphore(IMPORT_CONCURRENCY)
        # Per-(hash, album) locks close the TOCTOU race where two uploads of
        # identical content both pass the dedup check before either has
        # inserted its row -- see _do_upload. Safe to grow/read without its
        # own lock: the check-and-create below has no `await` in between, so
        # it's atomic under asyncio's cooperative scheduling.
        self._upload_locks: dict[tuple[str, int | None], asyncio.Lock] = {}
        # Lets a still-running download be cancelled from the API -- keyed by
        # the same job_id the frontend polls for progress.
        self._download_tasks: dict[str, asyncio.Task] = {}
        # See resolve_message -- avoids re-resolving the entity/message for
        # every single Range request a streamed video issues.
        self._message_cache: dict[int, object] = {}
        # See claim_thumbnail_probe/release_thumbnail_probe -- tracks which
        # files currently have an opportunistic thumbnail capture in
        # flight, so two overlapping Range requests for the same video
        # don't both try to generate one at once. Not a permanent record
        # of files that were ever attempted -- entries are removed once
        # that attempt finishes, win or lose, so a later request (e.g. the
        # real playback fetch right behind an initial tiny metadata probe)
        # gets its own turn.
        self._thumbnail_probe_attempted: set[int] = set()
        # Auto-sync once an "api"-sourced (phone) upload batch settles --
        # same debounce shape as keepawake's stop timer (and reuses its
        # grace period) since a batch is a sequence of independent requests
        # with real gaps between them, not one atomic operation.
        self._api_batch_active_count = 0
        self._api_batch_sync_task: asyncio.Task | None = None

    # --- lifecycle -----------------------------------------------------

    async def start(self) -> AuthState:
        """Called once at app startup: try to resume an existing session
        without prompting the user. Which api_id/api_hash to reconnect with
        comes from db.api_credentials, keyed by last_authorized_phone --
        Telethon sessions are tied to the api_id that created them, so this
        has to be the same pair used at login, not just any saved
        credentials."""
        session_str = secrets_store.get_session_string() or ""
        last_phone_key = config.get("last_authorized_phone")
        creds = db.get_api_credentials_for_phone(last_phone_key) if last_phone_key else None

        migrating_legacy = False
        if creds is None and session_str:
            # Installs from before per-phone-number credential binding
            # existed stored one api_id/api_hash pair globally, forever.
            # First launch after upgrading, there's an active session but
            # no db.api_credentials row yet -- migrate the legacy pair in
            # below, once we can confirm (via the session itself) which
            # phone number it actually belongs to.
            legacy = secrets_store.get_legacy_api_credentials()
            if legacy is not None:
                creds = legacy
                migrating_legacy = True

        if creds is None or not session_str:
            self.state = AuthState.NEEDS_PHONE
            return self.state

        api_id, api_hash = creds
        self.client = TelegramClient(StringSession(session_str), api_id, api_hash)
        await self.client.connect()

        if await self.client.is_user_authorized():
            if migrating_legacy:
                me = await self.client.get_me()
                phone_key = _phone_key(me.phone or "")
                if phone_key:
                    db.bind_api_credentials(phone_key, api_id, api_hash)
                    config.set("last_authorized_phone", phone_key)
                    config.set("last_authorized_user_id", me.id)
                secrets_store.clear_legacy_api_credentials()
            self.state = AuthState.AUTHORIZED
            await self._post_auth_setup()
        else:
            self.state = AuthState.NEEDS_PHONE
        return self.state

    async def disconnect(self) -> None:
        if self.client:
            await self.client.disconnect()

    # --- auth flow -------------------------------------------------------

    async def send_code(self, phone: str) -> AuthState:
        """First step of every login: given a phone number, either it's
        already bound to an api_id/api_hash (a returning number -- reuse
        those silently and go straight to requesting a code) or it isn't
        (first time this app has seen this number -- stash it and ask the
        setup wizard for credentials to bind, via set_credentials below)."""
        key = _phone_key(phone)
        creds = db.get_api_credentials_for_phone(key)
        if creds is None:
            self._pending_phone = phone
            self.state = AuthState.NEEDS_CREDENTIALS
            return self.state
        api_id, api_hash = creds
        return await self._request_code(phone, api_id, api_hash)

    async def set_credentials(self, api_id: int, api_hash: str) -> AuthState:
        """Only valid right after send_code has stashed a pending phone
        number that turned out to have no api_id/api_hash bound yet. Binds
        this pair to that phone number permanently, then immediately
        requests the login code the same way send_code does for a
        returning number. Refuses an api_id already bound to a *different*
        phone number outright -- allowing that would mean two different
        Telegram accounts silently sharing one app identity with no
        re-confirmation, the same kind of silent-carryover risk the
        account-switch index wipe (_reset_for_new_account) already guards
        against, just one step earlier in the flow."""
        if not self._pending_phone:
            raise ValueError("No phone number pending -- enter your phone number first")
        key = _phone_key(self._pending_phone)
        existing_phone_key = db.get_phone_for_api_id(api_id)
        if existing_phone_key is not None and existing_phone_key != key:
            raise ValueError("This API ID is already used by another phone number")
        db.bind_api_credentials(key, api_id, api_hash)
        phone = self._pending_phone
        self._pending_phone = None
        return await self._request_code(phone, api_id, api_hash)

    async def _request_code(self, phone: str, api_id: int, api_hash: str) -> AuthState:
        self.client = TelegramClient(StringSession(), api_id, api_hash)
        await self.client.connect()
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

    def _reset_for_new_account(self) -> None:
        """Wipes the local index and every account-scoped/auto-upload
        setting -- called only when the account that just authenticated is
        genuinely different from the one last remembered (see
        _finish_login), never on every login. A different account can't
        access the previous account's channels, so the old index is stale;
        and auto-upload/auto-delete features (Automatic Photos Backup, the
        watched folder) must never silently carry over to a new account
        with zero re-confirmation -- that's exactly how someone's photos
        can end up in the wrong Telegram account after a switch."""
        db.wipe_local_index()
        # wipe_local_index() is pure SQL by design -- it never touches the
        # actual cached bytes on disk, so without this every wipe would
        # silently orphan its entire media_cache/thumbnails footprint
        # forever (confirmed happening: 2.8GB of leftover cached files with
        # no DB row still pointing at them, found after exactly this path).
        removed = media.prune_orphaned_cache(db.get_all_known_hashes())
        if removed:
            logger.info("Pruned %d orphaned cache file(s) after account switch", removed)
        config.set("storage_channel_id", None)
        config.set("iphone_backup_album_id", None)
        config.set("photos_backup_enabled", False)
        config.set("photos_backup_album_id", None)
        config.set("watch_folder", None)
        # A completed backfill pass was only ever about the previous
        # account's videos -- the new account needs its own pass once its
        # own index is rebuilt (see backfill_video_thumbnails).
        config.set("thumbnail_backfill_done", False)

    async def _finish_login(self) -> None:
        secrets_store.set_session_string(self.client.session.save())
        self.state = AuthState.AUTHORIZED
        me = await self.client.get_me()
        last_user_id = config.get("last_authorized_user_id")
        if last_user_id is not None and last_user_id != me.id:
            logger.info(
                "Different Telegram account signed in (was %s, now %s) -- "
                "resetting local index and auto-upload settings", last_user_id, me.id,
            )
            self._reset_for_new_account()
        config.set("last_authorized_user_id", me.id)
        config.set("last_authorized_phone", _phone_key(self._phone or ""))
        await self._post_auth_setup()

    async def logout(self) -> AuthState:
        """Signs out: disconnects and clears the saved session only.
        Nothing in Telegram itself is touched.

        Deliberately does *not* wipe the local index, account-scoped
        settings, or the api_credentials binding registry -- logout() has
        no way to know whether the same account is about to sign back in
        (the common case) or a different one, so it can't safely decide
        that. That decision happens in _finish_login, which does know,
        once the newly-authenticated account's identity is available to
        compare. last_authorized_phone is deliberately left in config
        (not cleared here) so send_code recognizes a same-number relogin
        and skips straight to requesting a code, same as before -- and so
        _finish_login can still tell a same-account relogin apart from a
        genuine switch.

        No client is constructed here -- send_code builds one once it
        knows which api_id/api_hash the next phone number actually needs
        (a returning number's own binding, or freshly-entered credentials
        for a new one), rather than assuming whichever pair logout() last
        happened to have on hand."""
        if self.client:
            await self.client.disconnect()
        secrets_store.clear_session()
        # Rotate the pairing token on the way out: it gates the entire local
        # API, and until now it was minted once and reused forever, so a copy
        # that leaked (e.g. read out of the page by injected script) stayed
        # valid indefinitely. Logging out is the natural point to invalidate
        # every outstanding copy. The UI re-reads the new one over the
        # pywebview bridge on its next request.
        secrets_store.rotate_pairing_token()
        self.jobs.clear()
        self._message_cache.clear()
        self.client = None
        self._phone = None
        self._phone_code_hash = None
        self._pending_phone = None
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
            # new account, or a different account just signed in and
            # _finish_login's account-switch check wiped the index. Scan
            # for channels this account already owns
            # that BackitSnappy previously created and rebuild the local
            # index from them before falling back to creating fresh ones,
            # so logging back into an account that had content gets it all
            # back automatically.
            await self._discover_existing_channels()
            await self.sync_all()
        await self.ensure_storage_channel()
        await self.ensure_iphone_backup_album()
        if not config.get("thumbnail_backfill_done"):
            # Fire-and-forget: paced and low-priority (see
            # THUMBNAIL_BACKFILL_PACING_SECONDS), never awaited here so it
            # doesn't delay login completing or the UI becoming usable.
            asyncio.create_task(self.backfill_video_thumbnails())

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
        # _discover_existing_channels (called earlier in _post_auth_setup,
        # whenever storage_channel_id was unset) may have already found and
        # registered this account's real "iPhone Backup" channel under a
        # different local album_id -- reuse it rather than blindly creating
        # a second, duplicate Telegram channel just because the config
        # pointer to it happened to be unset (e.g. after an account-switch
        # reset that ran on a different login than the one that last set
        # storage_channel_id).
        for album in db.list_albums():
            if album["name"] == IPHONE_BACKUP_ALBUM_NAME:
                config.set("iphone_backup_album_id", album["id"])
                return album["id"]
        result = await self.create_album(IPHONE_BACKUP_ALBUM_NAME)
        config.set("iphone_backup_album_id", result["id"])
        return result["id"]

    async def ensure_photos_backup_album(self) -> int:
        """Same idempotent pattern as ensure_iphone_backup_album -- gives
        Automatic Photos Backup its own dedicated, browsable album instead
        of landing in the storage channel, which has no view in the UI at
        all. Created lazily (on first use) rather than at every startup,
        since the feature is off by default."""
        album_id = config.get("photos_backup_album_id")
        if album_id is not None and db.get_album(album_id) is not None:
            return album_id
        result = await self.create_album(PHOTOS_BACKUP_ALBUM_NAME)
        config.set("photos_backup_album_id", result["id"])
        return result["id"]

    # --- uploads -----------------------------------------------------------

    def start_upload(
        self, path: str | Path, album_id: int | None = None, source: str = "manual", delete_after: bool = False,
        queue_id: int | None = None, on_confirmed: Callable[[int], None] | None = None,
    ) -> str:
        """Schedule an upload from within the manager's own event loop
        (e.g. a FastAPI route handler) and return a job_id immediately.
        queue_id, if given, is a durable upload_queue row (see db.py) that
        gets removed once this upload reaches a terminal state -- rows
        still present on the next app launch are resumed from disk.
        on_confirmed, if given, is called with the new files.id only once a
        real DB row backed by a confirmed Telegram message id exists --
        never on failure or an ambiguous outcome. See _do_upload."""
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"percent": 0, "status": "queued", "filename": Path(path).name}
        asyncio.create_task(
            self._do_upload(job_id, Path(path), album_id, source, delete_after, queue_id, on_confirmed)
        )
        return job_id

    def queue_upload_threadsafe(
        self, path: str | Path, album_id: int | None = None, source: str = "watcher", delete_after: bool = False,
        queue_id: int | None = None, on_confirmed: Callable[[int], None] | None = None,
    ) -> str:
        """Thread-safe variant for callers outside the event loop (folder watcher)."""
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"percent": 0, "status": "queued", "filename": Path(path).name}
        asyncio.run_coroutine_threadsafe(
            self._do_upload(job_id, Path(path), album_id, source, delete_after, queue_id, on_confirmed),
            self._loop,
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

    def _notify_confirmed(self, on_confirmed: Callable[[int], None] | None, file_id: int) -> None:
        """Fires on_confirmed for a file_id backed by a real DB row (which
        only ever gets inserted after Telegram actually returned a message
        id, whether fresh, forwarded, or an already-deduped existing row) --
        wrapped so a bug in the caller's callback (e.g. the iCloud offload
        quarantine step) can never affect this job's own success reporting."""
        if on_confirmed is None:
            return
        try:
            on_confirmed(file_id)
        except Exception:
            logger.exception("on_confirmed callback failed for file %s", file_id)

    async def _do_upload(
        self, job_id: str, path: Path, album_id: int | None, source: str, delete_after: bool = False,
        queue_id: int | None = None, on_confirmed: Callable[[int], None] | None = None,
    ) -> None:
        job = self.jobs[job_id]
        try:
            if source in ("api", "photos_backup"):
                # api (iPhone Shortcuts) and photos_backup both run
                # unattended, on someone else's schedule (a phone tapping a
                # Shortcut, or an unattended poll timer) -- unlike a local
                # watched-folder drop, there's no guarantee anyone's at the
                # Mac to notice it fall asleep mid-upload.
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
                    self._notify_confirmed(on_confirmed, existing_here["id"])
                    return

                storage_copy = db.get_storage_copy_by_hash(sha256_hash)
                if storage_copy and album_id is not None:
                    job["status"] = "forwarding"
                    file_id = await self._forward_into_album(sha256_hash, storage_copy, album_id, source)
                    job.update(percent=100, status="done", file_id=file_id, deduped=True)
                    self._notify_confirmed(on_confirmed, file_id)
                    return

                job["status"] = "uploading"
                file_id = await self._upload_fresh(job_id, path, size, sha256_hash, mime_type, album_id, source)
                job.update(percent=100, status="done", file_id=file_id, deduped=False)
                self._notify_confirmed(on_confirmed, file_id)
        except Exception as exc:
            logger.exception("Upload failed for %s", path)
            job.update(status="error", error=str(exc))
        finally:
            if source in ("api", "photos_backup"):
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

        new_messages = [
            msg for msg in live_messages if msg.id not in tracked_message_ids and msg.file
        ]

        async def _import_one(msg) -> bool:
            async with self._import_semaphore:
                try:
                    imported = await self._import_message(msg, channel_id, album_id)
                    await asyncio.sleep(IMPORT_PACING_SECONDS)
                    return imported
                except Exception:
                    logger.exception("Failed to import message %s from channel %s", msg.id, channel_id)
                    return False

        results = await asyncio.gather(*(_import_one(msg) for msg in new_messages))
        added = sum(1 for imported in results if imported)

        return {"removed": removed, "added": added}

    async def _download_range(self, msg, offset: int, num_bytes: int) -> bytes:
        chunks = []
        async for chunk in self.client.iter_download(msg, offset=offset, request_size=num_bytes, limit=1):
            chunks.append(bytes(chunk))
        return b"".join(chunks)

    async def _hash_remote_file(self, msg, size: int) -> str:
        """Same fingerprint formula as the module-level _hash_file, but
        computed via two small range downloads (Telegram supports
        arbitrary byte-offset reads -- the same mechanism that lets a
        video be scrubbed without downloading it first) instead of
        pulling the entire file. This is what makes sync/import cheap;
        see PARTIAL_HASH_SAMPLE_BYTES's docstring for why this stays
        consistent with fresh, locally-hashed uploads."""
        h = hashlib.sha256()
        h.update(await self._download_range(msg, 0, PARTIAL_HASH_SAMPLE_BYTES))
        if size > PARTIAL_HASH_SAMPLE_BYTES:
            h.update(await self._download_range(msg, size - PARTIAL_HASH_SAMPLE_BYTES, PARTIAL_HASH_SAMPLE_BYTES))
        h.update(str(size).encode())
        return h.hexdigest()

    async def _import_message(self, msg, channel_id: int, album_id: int | None) -> bool:
        """Pull a Telegram-only message into the local index *without*
        downloading its full content -- a partial content fingerprint
        (two small range reads, see _hash_remote_file) plus Telegram's own
        small thumbnail preview are enough to index it safely. The full
        original is left to be pulled on demand later (ensure_local_media)
        the first time it's actually opened, rather than eagerly caching
        every synced-in file locally. Returns whether it was imported
        (False if it turned out to already be a known duplicate)."""
        # msg.file.name is chosen by whoever uploaded the file -- anyone
        # invited to a shared album can. Normalize it here, at the point it
        # enters the app, so a hostile name never reaches the database (and
        # from there the UI and every path-building call site) in the first
        # place; the individual path sinks sanitize again independently.
        filename = media.safe_filename(
            msg.file.name or f"telegram_{msg.id}{mimetypes.guess_extension(msg.file.mime_type or '') or ''}"
        )
        size = msg.file.size
        sha256_hash = await self._hash_remote_file(msg, size)

        if db.get_file_by_hash_and_album(sha256_hash, album_id):
            return False  # already tracked, somehow, under a different message id

        mime_type = msg.file.mime_type
        await self._ensure_thumbnail_fast(msg, sha256_hash)

        self._insert_file_safely(
            sha256_hash=sha256_hash,
            album_id=album_id,
            filename=filename,
            size=size,
            mime_type=mime_type,
            telegram_message_id=msg.id,
            channel_id=channel_id,
            source="telegram",
        )
        return True

    async def _ensure_thumbnail_fast(self, msg, sha256_hash: str) -> None:
        """Prefer Telegram's own pre-generated low-res preview (a tiny,
        separate download, `thumb=-1` picks the best available one) --
        much cheaper than downloading the full file to generate one
        locally, especially for videos where ffmpeg has real subprocess
        overhead. If Telegram doesn't have one for this message, the
        thumbnail stays missing (icon-only in the UI) until the file is
        actually opened -- ensure_local_media generates one then, once a
        full local copy exists to generate it from."""
        dest = media.thumbnail_path(sha256_hash)
        if dest.exists():
            return
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            thumb_tmp_target = dest.parent / f"{sha256_hash}.thumbpart"
            # Same "download_media may pick its own extension" caveat as
            # elsewhere -- use the returned path, not the one requested.
            downloaded = await self.client.download_media(msg, thumb=-1, file=str(thumb_tmp_target))
            if downloaded:
                Path(downloaded).replace(dest)
        except Exception:
            logger.exception("Fast thumbnail download failed for message %s", msg.id)

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
        self._message_cache.pop(file_id, None)

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

    async def resolve_message(self, file_row):
        """Fetches the live Telegram message backing a file row -- shared
        by ensure_local_media (full download) and the /stream route
        (ranged reads) so both resolve the same way. Cached per file_id:
        streaming a video issues one call per Range request (every seek,
        every buffered chunk), and re-resolving the entity + message from
        scratch each time was pure added latency on top of the actual byte
        fetch. Invalidated in delete_file so a removed file can't serve a
        stale reference."""
        file_id = file_row["id"]
        cached = self._message_cache.get(file_id)
        if cached is not None:
            return cached
        entity = await self.client.get_entity(file_row["channel_id"])
        message = await self.client.get_messages(entity, ids=file_row["telegram_message_id"])
        if message is None:
            raise ValueError("Original Telegram message no longer exists")
        self._message_cache[file_id] = message
        return message

    def claim_thumbnail_probe(self, file_id: int) -> bool:
        """Returns True (and marks a capture in-flight) only if no capture
        for this file_id is already running -- guards against two
        concurrent Range requests for the same video both trying to
        generate a thumbnail at once, not a permanent one-shot claim. The
        caller MUST call release_thumbnail_probe when its attempt finishes
        (success or failure), via /stream's `finally`. This used to be a
        true one-shot ("first caller ever wins, forever") which caused a
        real bug: browsers commonly issue a tiny metadata-probing Range
        request (a couple bytes) before the real playback request that
        actually buffers megabytes -- if that tiny probe claimed the one
        shot, it captured a near-empty buffer, ffmpeg failed on it, and
        the real request right behind it (which would have captured
        plenty) never got a turn. Releasing after every attempt lets the
        next request -- typically the real one -- retry with more data."""
        if file_id in self._thumbnail_probe_attempted:
            return False
        self._thumbnail_probe_attempted.add(file_id)
        return True

    def release_thumbnail_probe(self, file_id: int) -> None:
        self._thumbnail_probe_attempted.discard(file_id)

    async def _fetch_chunk_with_retry(self, message, offset: int, request_size: int) -> bytes:
        """Single-chunk fetch with retry -- GetFileRequest intermittently
        raises LimitInvalidError on a perfectly valid (request_size,
        offset) pair for reasons that don't reproduce reliably (confirmed
        directly: the exact same call can fail once and succeed moments
        later), so a bare retry is the practical fix rather than chasing a
        "correct" request_size."""
        last_exc: Exception | None = None
        for attempt in range(THUMBNAIL_BACKFILL_FETCH_MAX_ATTEMPTS):
            try:
                async for chunk in self.client.iter_download(
                    message, offset=offset, request_size=request_size, limit=1
                ):
                    return bytes(chunk)
                return b""
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Backfill chunk fetch at offset %d failed (attempt %d/%d): %s",
                    offset, attempt + 1, THUMBNAIL_BACKFILL_FETCH_MAX_ATTEMPTS, exc,
                )
                if attempt + 1 < THUMBNAIL_BACKFILL_FETCH_MAX_ATTEMPTS:
                    await asyncio.sleep(THUMBNAIL_BACKFILL_FETCH_RETRY_DELAY_SECONDS)
        raise last_exc

    async def _fetch_byte_range(self, message, start: int, length: int, request_size: int = 262144) -> bytes:
        """Fetches exactly `length` bytes starting at `start`, sequentially
        (no concurrency -- this backs low-priority background work, not
        interactive playback). Aligns the first request down to a
        request_size boundary and trims the leading overlap: Telegram's
        GetFileRequest requires the offset itself to be a multiple of
        request_size, confirmed live -- an unaligned offset can reliably
        fail with LimitInvalidError even though the exact same range,
        aligned down, succeeds immediately."""
        if length <= 0:
            return b""
        aligned_start = (start // request_size) * request_size
        leading_trim = start - aligned_start
        collected = bytearray()
        offset = aligned_start
        while len(collected) < leading_trim + length:
            chunk = await self._fetch_chunk_with_retry(message, offset, request_size)
            if not chunk:
                break
            collected.extend(chunk)
            offset += request_size
        return bytes(collected[leading_trim:leading_trim + length])

    async def _backfill_one_thumbnail(self, file_row) -> bool:
        file_size = file_row["size"]
        try:
            message = await self.resolve_message(file_row)
        except ValueError:
            return False
        head = await self._fetch_byte_range(message, 0, min(THUMBNAIL_BACKFILL_HEAD_BYTES, file_size))
        for tail_size in THUMBNAIL_BACKFILL_TAIL_SIZES:
            tail_size = min(tail_size, file_size)
            tail_start = max(0, file_size - tail_size)
            tail = await self._fetch_byte_range(message, tail_start, file_size - tail_start)
            ok = await asyncio.to_thread(
                media.generate_video_thumbnail_from_head_tail,
                head, tail, tail_start, file_size, file_row["sha256_hash"], file_row["mime_type"],
            )
            if ok:
                return True
            if tail_start == 0:
                break  # already fetched from the true start -- a bigger tail can't find more
        return False

    async def backfill_video_thumbnails(self) -> None:
        """One-time sweep: generates a thumbnail for every already-indexed
        video that doesn't have one yet, using head+tail range fetches
        (see media.generate_video_thumbnail_from_head_tail) instead of a
        full download -- covers camera-original footage whose moov
        (index) atom sits at the end of the file, which the per-stream
        opportunistic capture (see claim_thumbnail_probe) can never solve
        no matter how much of the head it captures. Runs once, ever:
        thumbnails now persist across logout/relogin (see
        _reset_for_new_account/wipe_local_index, which never touch them),
        so there's no recurring reason to repeat this beyond one pass --
        config['thumbnail_backfill_done'] tracks that. Safe to interrupt
        (an app restart) and resume, since it just re-scans for files
        still missing a thumbnail; safe to call more than once, since a
        completed pass is a no-op."""
        if config.get("thumbnail_backfill_done"):
            return
        candidates = list(db.list_files(album_id=None))
        for album in db.list_albums():
            candidates.extend(db.list_files(album_id=album["id"]))
        candidates = [
            row for row in candidates
            if media.classify(row["mime_type"]) == "video"
            and not media.thumbnail_path(row["sha256_hash"]).exists()
        ]
        if not candidates:
            config.set("thumbnail_backfill_done", True)
            return
        logger.info("Thumbnail backfill starting: %d video(s) without a thumbnail", len(candidates))
        made = 0
        for i, row in enumerate(candidates):
            try:
                if await self._backfill_one_thumbnail(row):
                    made += 1
            except Exception:
                logger.exception("Thumbnail backfill failed for file %s", row["id"])
            if i + 1 < len(candidates):
                await asyncio.sleep(THUMBNAIL_BACKFILL_PACING_SECONDS)
        logger.info(
            "Thumbnail backfill complete: %d of %d generated (rest have no moov atom "
            "within the fetched tail, or another decode failure)", made, len(candidates),
        )
        config.set("thumbnail_backfill_done", True)

    async def ensure_local_media(self, file_row, job_id: str | None = None) -> Path:
        """Return a fully-downloaded local copy of this file, downloading
        from Telegram into the media cache first if not already cached."""
        dest = media.cached_media_path(file_row["sha256_hash"], file_row["filename"])
        if dest.exists():
            media.touch_cache_access(dest)
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)

        message = await self.resolve_message(file_row)

        def _progress(current: int, total: int) -> None:
            if job_id and job_id in self.jobs:
                self.jobs[job_id]["percent"] = int(current * 100 / total) if total else 0

        tmp_dest = dest.with_name(dest.name + ".part")
        await self.client.download_media(message, file=str(tmp_dest), progress_callback=_progress)
        tmp_dest.rename(dest)
        await asyncio.to_thread(media.enforce_cache_limit)

        # A synced-in file (source="telegram") skips the full download at
        # import time, so it may not have a thumbnail yet if Telegram had
        # no preview of its own for that message. Now that a full local
        # copy exists, generate one the normal way rather than leaving it
        # icon-only forever.
        if not media.thumbnail_path(file_row["sha256_hash"]).exists():
            try:
                await asyncio.to_thread(
                    media.generate_thumbnail, dest, file_row["sha256_hash"], file_row["mime_type"]
                )
            except Exception:
                logger.exception("On-demand thumbnail generation failed for file %s", file_row["id"])
        return dest

    def start_prepare(self, file_id: int) -> str:
        """Ensure a file is downloaded to the local media cache (for lightbox
        viewing) and return a job_id to poll for progress. Registered in
        _download_tasks (shared with start_download) so the existing
        cancel_download() also covers a prepare job -- e.g. the lightbox
        closing before a video finishes downloading."""
        file_row = db.get_file(file_id)
        if file_row is None:
            raise ValueError("File not found")
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"percent": 0, "status": "queued", "filename": file_row["filename"]}
        task = asyncio.create_task(self._do_prepare(job_id, file_row))
        self._download_tasks[job_id] = task
        return job_id

    async def _do_prepare(self, job_id: str, file_row) -> None:
        job = self.jobs[job_id]
        try:
            job["status"] = "downloading"
            await self.ensure_local_media(file_row, job_id=job_id)
            job.update(percent=100, status="done")
        except asyncio.CancelledError:
            job.update(status="error", error="Cancelled")
            cache_dest = media.cached_media_path(file_row["sha256_hash"], file_row["filename"])
            cache_dest.with_name(cache_dest.name + ".part").unlink(missing_ok=True)
            raise
        except Exception as exc:
            logger.exception("Prepare failed for file %s", file_row["id"])
            job.update(status="error", error=str(exc))
        finally:
            self._download_tasks.pop(job_id, None)

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
                # An exact file path chosen via the single-file Save As
                # panel -- the filename itself is already user-confirmed,
                # so no collision-suffixing here.
                if not destination_path:
                    raise ValueError("No destination path given")
                dest = _validated_save_path(destination_path)
            elif destination_type == "folder":
                # A directory chosen once (via the native folder picker) and
                # reused for every file in a multi-select download -- same
                # Finder-style collision handling as the default path below,
                # just rooted at the user's chosen folder instead of
                # ~/Downloads/BackitSnappy.
                if not destination_path:
                    raise ValueError("No destination folder given")
                folder = _validated_save_path(destination_path)
                folder.mkdir(parents=True, exist_ok=True)
                dest = _unique_path(folder / media.safe_filename(file_row["filename"]))
            else:
                downloads_dir = Path.home() / "Downloads" / "BackitSnappy"
                downloads_dir.mkdir(parents=True, exist_ok=True)
                # safe_filename, not the raw name: this is Telegram-supplied
                # and would otherwise escape downloads_dir (see its docstring).
                dest = _unique_path(downloads_dir / media.safe_filename(file_row["filename"]))

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

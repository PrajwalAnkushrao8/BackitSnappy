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
import uuid
from pathlib import Path

from telethon import TelegramClient, functions
from telethon.errors import (
    PeerFloodError,
    SessionPasswordNeededError,
    UserPrivacyRestrictedError,
)
from telethon.sessions import StringSession

from .. import config, db, secrets_store
from .auth_flow import AuthState

logger = logging.getLogger(__name__)

STORAGE_CHANNEL_TITLE = "BackitSnappy Storage"
DEFAULT_UPLOAD_CAP_BYTES = 2 * 1024 * 1024 * 1024
PREMIUM_UPLOAD_CAP_BYTES = 4 * 1024 * 1024 * 1024


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class TelegramManager:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop
        self.client: TelegramClient | None = None
        self.state: AuthState = AuthState.NEEDS_CREDENTIALS
        self._phone: str | None = None
        self._phone_code_hash: str | None = None
        self.upload_cap_bytes: int = DEFAULT_UPLOAD_CAP_BYTES
        self.jobs: dict[str, dict] = {}

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

    async def _post_auth_setup(self) -> None:
        # StringSession doesn't persist entity access_hashes across restarts,
        # so re-fetch dialogs each start to warm the in-memory cache before
        # resolving any previously-stored channel IDs.
        try:
            await self.client.get_dialogs()
        except Exception:
            logger.exception("Failed to warm entity cache via get_dialogs")
        await self._refresh_upload_cap()
        await self.ensure_storage_channel()

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

    # --- uploads -----------------------------------------------------------

    def start_upload(
        self, path: str | Path, album_id: int | None = None, source: str = "manual", delete_after: bool = False
    ) -> str:
        """Schedule an upload from within the manager's own event loop
        (e.g. a FastAPI route handler) and return a job_id immediately."""
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"percent": 0, "status": "queued", "filename": Path(path).name}
        asyncio.create_task(self._do_upload(job_id, Path(path), album_id, source, delete_after))
        return job_id

    def queue_upload_threadsafe(
        self, path: str | Path, album_id: int | None = None, source: str = "watcher", delete_after: bool = False
    ) -> str:
        """Thread-safe variant for callers outside the event loop (folder watcher)."""
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {"percent": 0, "status": "queued", "filename": Path(path).name}
        asyncio.run_coroutine_threadsafe(
            self._do_upload(job_id, Path(path), album_id, source, delete_after), self._loop
        )
        return job_id

    async def _do_upload(
        self, job_id: str, path: Path, album_id: int | None, source: str, delete_after: bool = False
    ) -> None:
        job = self.jobs[job_id]
        try:
            size = path.stat().st_size
            if size > self.upload_cap_bytes:
                cap_gb = self.upload_cap_bytes // (1024**3)
                raise ValueError(f"File exceeds the {cap_gb}GB limit for this Telegram account")

            job["status"] = "hashing"
            sha256_hash = await asyncio.to_thread(_hash_file, path)

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
            file_id = await self._upload_fresh(job_id, path, size, sha256_hash, album_id, source)
            job.update(percent=100, status="done", file_id=file_id, deduped=False)
        except Exception as exc:
            logger.exception("Upload failed for %s", path)
            job.update(status="error", error=str(exc))
        finally:
            if delete_after:
                path.unlink(missing_ok=True)

    async def _forward_into_album(self, sha256_hash: str, storage_copy, album_id: int, source: str) -> int:
        album = db.get_album(album_id)
        storage_entity = await self.client.get_entity(config.get("storage_channel_id"))
        album_entity = await self.client.get_entity(album["telegram_channel_id"])
        [forwarded] = await self.client.forward_messages(
            album_entity, [storage_copy["telegram_message_id"]], from_peer=storage_entity
        )
        return db.insert_file(
            filename=storage_copy["filename"],
            sha256_hash=sha256_hash,
            size=storage_copy["size"],
            mime_type=storage_copy["mime_type"],
            telegram_message_id=forwarded.id,
            channel_id=album["telegram_channel_id"],
            album_id=album_id,
            source=source,
        )

    async def _upload_fresh(
        self, job_id: str, path: Path, size: int, sha256_hash: str, album_id: int | None, source: str
    ) -> int:
        if album_id is not None:
            target_channel_id = db.get_album(album_id)["telegram_channel_id"]
        else:
            target_channel_id = config.get("storage_channel_id")
        entity = await self.client.get_entity(target_channel_id)

        def _progress(current: int, total: int) -> None:
            self.jobs[job_id]["percent"] = int(current * 100 / total) if total else 0

        message = await self.client.send_file(entity, str(path), progress_callback=_progress)
        return db.insert_file(
            filename=path.name,
            sha256_hash=sha256_hash,
            size=size,
            mime_type=mimetypes.guess_type(path.name)[0],
            telegram_message_id=message.id,
            channel_id=target_channel_id,
            album_id=album_id,
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

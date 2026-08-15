"""Non-secret application settings, persisted as JSON under Application Support.

Secrets (API credentials, Telethon session, pairing token) live in the macOS
Keychain via secrets_store.py, never here.
"""
import json
import threading
from pathlib import Path

APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "BackitSnappy"
CONFIG_PATH = APP_SUPPORT_DIR / "config.json"
DB_PATH = APP_SUPPORT_DIR / "backitsnappy.db"

DEFAULTS = {
    "local_port": 8765,
    "storage_channel_id": None,
    "iphone_backup_album_id": None,
    "watch_folder": None,
    "onboarding_completed": False,
    "photos_backup_enabled": False,
    "photos_backup_poll_interval_minutes": 10,
    "photos_backup_album_id": None,
    "last_authorized_user_id": None,
    # Digits-only phone key (see client_manager._phone_key) of whoever is
    # currently/last signed in -- looked up against db's api_credentials
    # table on every app launch to reconnect with the same api_id/api_hash
    # that created the saved session. Survives logout (see
    # TelegramManager.logout) so the same-account-relogin fast path and
    # this reconnect-on-launch path both keep working.
    "last_authorized_phone": None,
    "media_cache_max_bytes": 5 * 1024**3,
    # See TelegramManager.backfill_video_thumbnails -- flips to True once
    # a full pass over every already-synced video has completed, so it
    # never repeats. Not account-scoped on purpose: even after
    # _reset_for_new_account wipes the file index for a genuine account
    # switch, there's nothing left to backfill until a new sync populates
    # it, and this flag being stale-True in that window is harmless (the
    # candidate list would just be empty anyway).
    "thumbnail_backfill_done": False,
}

_lock = threading.Lock()


def _ensure_dir() -> None:
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    _ensure_dir()
    with _lock:
        if not CONFIG_PATH.exists():
            data = dict(DEFAULTS)
            CONFIG_PATH.write_text(json.dumps(data, indent=2))
            return data
        data = json.loads(CONFIG_PATH.read_text())
        merged = dict(DEFAULTS)
        merged.update(data)
        return merged


def save(data: dict) -> None:
    _ensure_dir()
    with _lock:
        CONFIG_PATH.write_text(json.dumps(data, indent=2))


def get(key: str):
    return load().get(key, DEFAULTS.get(key))


def set(key: str, value) -> None:
    data = load()
    data[key] = value
    save(data)

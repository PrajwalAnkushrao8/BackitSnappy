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
    "tailscale_port": 8766,
    "storage_channel_id": None,
    "iphone_backup_album_id": None,
    "watch_folder": None,
    "tailscale_access_enabled": False,
    "onboarding_completed": False,
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

"""Secrets (Telegram API credentials, Telethon session, pairing token) stored
in the macOS Keychain via `keyring` — never written to disk in plaintext.
"""
import secrets

import keyring

SERVICE_NAME = "com.backitsnappy.app"

_KEY_API_ID = "api_id"
_KEY_API_HASH = "api_hash"
_KEY_SESSION = "telethon_session"
_KEY_PAIRING_TOKEN = "pairing_token"


def _api_hash_key(phone_key: str) -> str:
    return f"{_KEY_API_HASH}:{phone_key}"


def set_api_hash_for_phone(phone_key: str, api_hash: str) -> None:
    """Stores one phone number's api_hash in the Keychain.

    api_hash is a credential and belongs here, not in the SQLite index:
    that file sits in Application Support at default permissions, so
    anything running as this user (or any admin on the machine) can read
    it, whereas the Keychain is encrypted at rest and access-controlled.
    db.api_credentials keeps only the non-secret phone -> api_id mapping."""
    keyring.set_password(SERVICE_NAME, _api_hash_key(phone_key), api_hash)


def get_api_hash_for_phone(phone_key: str) -> str | None:
    return keyring.get_password(SERVICE_NAME, _api_hash_key(phone_key))


def get_legacy_api_credentials() -> tuple[int, str] | None:
    """Reads the single global api_id/api_hash pair from installs predating
    per-phone-number credential binding (see db.api_credentials) -- every
    account used to share one app-wide api_id/api_hash forever, set once
    via the old setup wizard. Only ever read once, by
    TelegramManager.start()'s one-time migration path, which binds it to
    whichever phone the still-active saved session actually belongs to and
    then calls clear_legacy_api_credentials(). New logins never write these
    keys again."""
    api_id = keyring.get_password(SERVICE_NAME, _KEY_API_ID)
    api_hash = keyring.get_password(SERVICE_NAME, _KEY_API_HASH)
    if not api_id or not api_hash:
        return None
    return int(api_id), api_hash


def clear_legacy_api_credentials() -> None:
    for key in (_KEY_API_ID, _KEY_API_HASH):
        try:
            keyring.delete_password(SERVICE_NAME, key)
        except keyring.errors.PasswordDeleteError:
            pass


def get_session_string() -> str | None:
    return keyring.get_password(SERVICE_NAME, _KEY_SESSION)


def set_session_string(session_string: str) -> None:
    keyring.set_password(SERVICE_NAME, _KEY_SESSION, session_string)


def clear_session() -> None:
    try:
        keyring.delete_password(SERVICE_NAME, _KEY_SESSION)
    except keyring.errors.PasswordDeleteError:
        pass


def get_pairing_token() -> str:
    token = keyring.get_password(SERVICE_NAME, _KEY_PAIRING_TOKEN)
    if not token:
        token = rotate_pairing_token()
    return token


def rotate_pairing_token() -> str:
    token = secrets.token_urlsafe(32)
    keyring.set_password(SERVICE_NAME, _KEY_PAIRING_TOKEN, token)
    return token


def clear_all() -> None:
    for key in (_KEY_API_ID, _KEY_API_HASH, _KEY_SESSION, _KEY_PAIRING_TOKEN):
        try:
            keyring.delete_password(SERVICE_NAME, key)
        except keyring.errors.PasswordDeleteError:
            pass

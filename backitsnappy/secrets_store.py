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


def has_api_credentials() -> bool:
    return bool(keyring.get_password(SERVICE_NAME, _KEY_API_ID)) and bool(
        keyring.get_password(SERVICE_NAME, _KEY_API_HASH)
    )


def get_api_credentials() -> tuple[int, str] | None:
    api_id = keyring.get_password(SERVICE_NAME, _KEY_API_ID)
    api_hash = keyring.get_password(SERVICE_NAME, _KEY_API_HASH)
    if not api_id or not api_hash:
        return None
    return int(api_id), api_hash


def set_api_credentials(api_id: int, api_hash: str) -> None:
    keyring.set_password(SERVICE_NAME, _KEY_API_ID, str(api_id))
    keyring.set_password(SERVICE_NAME, _KEY_API_HASH, api_hash)


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

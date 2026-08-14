import hmac

from fastapi import HTTPException, Query, Request, Security
from fastapi.security import APIKeyHeader

from .. import secrets_store
from ..telegram.client_manager import TelegramManager

_api_key_header = APIKeyHeader(name="X-Pairing-Token", auto_error=False)


def _check(token: str | None) -> bool:
    expected = secrets_store.get_pairing_token()
    return bool(token) and hmac.compare_digest(token, expected)


async def verify_pairing_token(token: str | None = Security(_api_key_header)) -> None:
    if not _check(token):
        raise HTTPException(status_code=401, detail="Invalid or missing pairing token")


async def verify_pairing_token_flexible(
    header_token: str | None = Security(_api_key_header),
    query_token: str | None = Query(default=None, alias="token"),
) -> None:
    """Header OR ?token= query param. Scoped to the media-serving routes
    only (thumbnail/full media) — browsers can't attach custom headers to
    <img>/<video> src requests, so those need a URL-carriable credential.
    Every other route stays strict header-only via verify_pairing_token."""
    if not (_check(header_token) or _check(query_token)):
        raise HTTPException(status_code=401, detail="Invalid or missing pairing token")


def get_manager(request: Request) -> TelegramManager:
    return request.app.state.telegram_manager

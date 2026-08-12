import hmac

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from .. import secrets_store
from ..telegram.client_manager import TelegramManager

_api_key_header = APIKeyHeader(name="X-Pairing-Token", auto_error=False)


async def verify_pairing_token(token: str | None = Security(_api_key_header)) -> None:
    expected = secrets_store.get_pairing_token()
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing pairing token")


def get_manager(request: Request) -> TelegramManager:
    return request.app.state.telegram_manager

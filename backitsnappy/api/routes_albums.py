from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db
from ..telegram.client_manager import TelegramManager
from .deps import get_manager

router = APIRouter()


class AlbumCreateIn(BaseModel):
    name: str


class InviteIn(BaseModel):
    username: str


def _album_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "telegram_channel_id": row["telegram_channel_id"],
        "created_at": row["created_at"],
    }


@router.get("")
async def list_albums():
    return [_album_to_dict(a) for a in db.list_albums()]


@router.post("")
async def create_album(body: AlbumCreateIn, manager: TelegramManager = Depends(get_manager)):
    try:
        return await manager.create_album(body.name)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{album_id}/invite")
async def invite(album_id: int, body: InviteIn, manager: TelegramManager = Depends(get_manager)):
    """Tries a direct Telegram invite first; if the invitee's privacy settings
    block that (common for non-mutual-contacts), falls back to an invite link."""
    try:
        return await manager.invite_to_album(album_id, body.username)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{album_id}/invite_link")
async def invite_link(album_id: int, manager: TelegramManager = Depends(get_manager)):
    try:
        link = await manager.get_invite_link(album_id)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"invite_link": link}

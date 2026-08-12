from fastapi import APIRouter, HTTPException

from .. import db

router = APIRouter()


def _file_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "sha256_hash": row["sha256_hash"],
        "size": row["size"],
        "mime_type": row["mime_type"],
        "telegram_message_id": row["telegram_message_id"],
        "channel_id": row["channel_id"],
        "album_id": row["album_id"],
        "source": row["source"],
        "uploaded_at": row["uploaded_at"],
    }


@router.get("")
async def list_files(album_id: int | None = None):
    return [_file_to_dict(f) for f in db.list_files(album_id=album_id)]


@router.get("/{file_id}")
async def get_file(file_id: int):
    row = db.get_file(file_id)
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return _file_to_dict(row)

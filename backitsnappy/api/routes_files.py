from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db, media
from ..telegram.client_manager import TelegramManager
from .deps import get_manager

router = APIRouter()


class DownloadIn(BaseModel):
    # "default" (~/Downloads/BackitSnappy/), "custom" (path is an exact file
    # path, from the single-file Save As panel), or "folder" (path is a
    # directory, reused with Finder-style collision handling for every file
    # in a multi-select download -- from the native folder picker).
    destination: str = "default"
    path: str | None = None  # required for "custom" and "folder"


def _file_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "filename": row["filename"],
        "sha256_hash": row["sha256_hash"],
        "size": row["size"],
        "mime_type": row["mime_type"],
        "media_type": media.classify(row["mime_type"]),
        "has_thumbnail": media.thumbnail_path(row["sha256_hash"]).exists(),
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


@router.post("/{file_id}/prepare")
async def prepare_file(file_id: int, manager: TelegramManager = Depends(get_manager)):
    """Ensures the file is downloaded to the local media cache (for lightbox
    viewing) and returns a job_id — poll it via the download-progress route
    below, shared between prepare and full-download jobs."""
    try:
        job_id = manager.start_prepare(file_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"job_id": job_id}


@router.post("/{file_id}/download")
async def download_file(
    file_id: int, body: DownloadIn, manager: TelegramManager = Depends(get_manager)
):
    if body.destination in ("custom", "folder") and not body.path:
        raise HTTPException(status_code=400, detail=f"path is required for a {body.destination} destination")
    try:
        job_id = manager.start_download(file_id, body.destination, body.path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"job_id": job_id}


@router.get("/{file_id}/download/{job_id}/progress")
async def download_progress(
    file_id: int, job_id: str, manager: TelegramManager = Depends(get_manager)
):
    job = manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return job


@router.post("/{file_id}/download/{job_id}/cancel")
async def cancel_download(
    file_id: int, job_id: str, manager: TelegramManager = Depends(get_manager)
):
    if not manager.cancel_download(job_id):
        raise HTTPException(status_code=404, detail="Job not found or already finished")
    return {"cancelled": True}


@router.delete("/{file_id}")
async def delete_file(file_id: int, manager: TelegramManager = Depends(get_manager)):
    try:
        await manager.delete_file(file_id)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if "not found" in detail.lower() else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return {"deleted": True}

"""Media-serving routes (thumbnails, full media for the lightbox/video
playback). Mounted with the flexible (header-or-query-token) auth
dependency in server.py, since browsers can't attach custom headers to
<img>/<video> src requests — every other route stays header-only.
"""
from fastapi import APIRouter, HTTPException
from starlette.responses import FileResponse

from .. import db, media

router = APIRouter()


@router.get("/{file_id}/thumbnail")
async def get_thumbnail(file_id: int):
    file_row = db.get_file(file_id)
    if file_row is None:
        raise HTTPException(status_code=404, detail="File not found")
    path = media.thumbnail_path(file_row["sha256_hash"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="No thumbnail available")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/{file_id}/media")
async def get_media(file_id: int):
    """Serves the full file for lightbox viewing/video playback, with
    Range-request support (FileResponse handles this automatically). The
    caller must have already called POST /{file_id}/prepare and polled it
    to completion — this route only serves what's already cached locally,
    it never triggers a Telegram download itself."""
    file_row = db.get_file(file_id)
    if file_row is None:
        raise HTTPException(status_code=404, detail="File not found")
    path = media.cached_media_path(file_row["sha256_hash"], file_row["filename"])
    if not path.exists():
        raise HTTPException(status_code=404, detail="Not downloaded yet -- call /prepare first")
    return FileResponse(path, media_type=file_row["mime_type"] or "application/octet-stream")

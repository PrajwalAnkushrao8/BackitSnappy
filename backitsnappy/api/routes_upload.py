import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .. import db
from ..telegram.client_manager import TelegramManager
from .deps import get_manager

router = APIRouter()

UPLOAD_TMP_DIR = Path(tempfile.gettempdir()) / "backitsnappy-uploads"


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    album_id: int | None = Form(None),
    manager: TelegramManager = Depends(get_manager),
):
    UPLOAD_TMP_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_TMP_DIR / f"{uuid.uuid4().hex}_{file.filename}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    # Durable from this point on -- if the app closes or crashes before this
    # upload finishes, the row (and the temp file it points at) survives to
    # be resumed on next launch. See client_manager._do_upload's finally
    # block for where it gets cleaned up on success/failure.
    queue_id = db.enqueue_upload(str(dest), file.filename, album_id, "api")
    job_id = manager.start_upload(dest, album_id=album_id, source="api", delete_after=True, queue_id=queue_id)
    return {"job_id": job_id}


@router.get("/upload/{job_id}/progress")
async def upload_progress(job_id: str, manager: TelegramManager = Depends(get_manager)):
    job = manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return job

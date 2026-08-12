import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

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

    job_id = manager.start_upload(dest, album_id=album_id, source="api", delete_after=True)
    return {"job_id": job_id}


@router.get("/upload/{job_id}/progress")
async def upload_progress(job_id: str, manager: TelegramManager = Depends(get_manager)):
    job = manager.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job_id")
    return job

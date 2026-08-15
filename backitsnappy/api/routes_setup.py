from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..telegram.client_manager import TelegramManager
from .deps import get_manager

router = APIRouter()


class CredentialsIn(BaseModel):
    api_id: int
    api_hash: str


class PhoneIn(BaseModel):
    phone: str


class CodeIn(BaseModel):
    code: str


class PasswordIn(BaseModel):
    password: str


@router.get("/status")
async def status(manager: TelegramManager = Depends(get_manager)):
    return {"state": manager.state.value}


@router.post("/credentials")
async def submit_credentials(body: CredentialsIn, manager: TelegramManager = Depends(get_manager)):
    try:
        state = await manager.set_credentials(body.api_id, body.api_hash)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"state": state.value}


@router.post("/phone")
async def submit_phone(body: PhoneIn, manager: TelegramManager = Depends(get_manager)):
    try:
        state = await manager.send_code(body.phone)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"state": state.value}


@router.post("/code")
async def submit_code(body: CodeIn, manager: TelegramManager = Depends(get_manager)):
    try:
        state = await manager.submit_code(body.code)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"state": state.value}


@router.post("/password")
async def submit_password(body: PasswordIn, manager: TelegramManager = Depends(get_manager)):
    try:
        state = await manager.submit_password(body.password)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"state": state.value}


@router.post("/logout")
async def logout(request: Request, manager: TelegramManager = Depends(get_manager)):
    try:
        state = await manager.logout()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # manager.logout() already reset watch_folder to None in config, but
    # that doesn't stop an already-running watcher on its own -- without
    # this, a folder watch started under the old account would keep
    # uploading anything dropped in it to whichever account logs in next.
    request.app.state.watcher_controller.restart(None)
    return {"state": state.value}

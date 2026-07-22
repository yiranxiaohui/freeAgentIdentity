from __future__ import annotations

from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from application.auto_register import auto_register_controller

router = APIRouter(prefix="/auto-register", tags=["auto-register"])


class AutoRegisterStartRequest(BaseModel):
    batch: int = 5
    concurrency: int = 3
    target: int = 0
    interval: int = 10
    executor_type: Optional[str] = None
    auto_import: bool = False


@router.get("/status")
def get_status():
    return auto_register_controller.status()


@router.post("/start")
def start(body: AutoRegisterStartRequest):
    return auto_register_controller.start(body.model_dump())


@router.post("/stop")
def stop():
    return auto_register_controller.stop()

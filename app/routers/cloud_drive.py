from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.cloud_drive_provider import get_cloud_drive, provider_label


router = APIRouter(prefix="/api/cloud_drive", tags=["CloudDrive"])


class CloudDriveConfigPayload(BaseModel):
    provider: str = "123pan"
    name: str = ""
    clouddrive_base_url: str = ""
    clouddrive_username: str = ""
    clouddrive_password: str = ""
    clouddrive_root_path: str = "/"
    clouddrive_direct_base_url: str = ""
    clouddrive_read_only: bool = False

    class Config:
        extra = "ignore"


class BrowsePayload(BaseModel):
    drive_index: int = 0
    path: str = "0"
    include_files: bool = False


def _raise_cloud_error(exc: Exception):
    raise HTTPException(status_code=400, detail=str(exc))


@router.post("/test")
def test_cloud_drive(payload: CloudDriveConfigPayload):
    try:
        data: dict[str, Any] = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
        cloud = get_cloud_drive(0, data)
        return cloud.test_connection()
    except Exception as e:
        _raise_cloud_error(e)


@router.post("/browse")
def browse_cloud_drive(payload: BrowsePayload):
    try:
        cloud = get_cloud_drive(payload.drive_index)
        return cloud.list(payload.path, include_files=payload.include_files)
    except Exception as e:
        return {
            "status": "error",
            "message": f"{provider_label('clouddrive2')} 浏览失败: {e}",
            "dirs": [],
            "files": [],
        }

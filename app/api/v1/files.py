"""
文件服务 API 路由
"""

import aiofiles.os
import re
from pathlib import Path
from urllib.parse import unquote
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.responses import FileResponse

from app.core.auth import (
    is_files_public,
    security,
    verify_api_key_required,
)
from app.core.logger import logger
from app.core.storage import DATA_DIR

router = APIRouter(tags=["Files"])

# 缓存根目录
BASE_DIR = DATA_DIR / "tmp"
IMAGE_DIR = BASE_DIR / "image"
VIDEO_DIR = BASE_DIR / "video"

FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,255}$")
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v"}


def _normalize_filename(raw: str) -> str:
    decoded = unquote((raw or "").strip())
    normalized = decoded.replace("\\", "-").replace("/", "-")
    if not normalized or ".." in normalized:
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not FILENAME_PATTERN.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return normalized


def _safe_resolve(base_dir: Path, filename: str) -> Path:
    base_resolved = base_dir.resolve()
    resolved = (base_resolved / filename).resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename")
    return resolved


async def verify_files_access(
    auth: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> Optional[str]:
    if is_files_public():
        return None
    return await verify_api_key_required(auth)


@router.get("/image/{filename:path}")
async def get_image(filename: str, _: Optional[str] = Depends(verify_files_access)):
    """
    获取图片文件
    """
    normalized_name = _normalize_filename(filename)
    file_path = _safe_resolve(IMAGE_DIR, normalized_name)
    if file_path.suffix.lower() not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported image extension")

    if await aiofiles.os.path.exists(file_path):
        if await aiofiles.os.path.isfile(file_path):
            content_type = "image/jpeg"
            if file_path.suffix.lower() == ".png":
                content_type = "image/png"
            elif file_path.suffix.lower() == ".webp":
                content_type = "image/webp"

            # 增加缓存头，支持高并发场景下的浏览器/CDN缓存
            return FileResponse(
                file_path,
                media_type=content_type,
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )

    logger.warning(f"Image not found: {normalized_name}")
    raise HTTPException(status_code=404, detail="Image not found")


@router.get("/video/{filename:path}")
async def get_video(filename: str, _: Optional[str] = Depends(verify_files_access)):
    """
    获取视频文件
    """
    normalized_name = _normalize_filename(filename)
    file_path = _safe_resolve(VIDEO_DIR, normalized_name)
    if file_path.suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Unsupported video extension")

    if await aiofiles.os.path.exists(file_path):
        if await aiofiles.os.path.isfile(file_path):
            return FileResponse(
                file_path,
                media_type="video/mp4",
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )

    logger.warning(f"Video not found: {normalized_name}")
    raise HTTPException(status_code=404, detail="Video not found")

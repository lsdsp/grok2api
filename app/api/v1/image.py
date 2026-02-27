"""
Image Generation API 路由
"""

import base64
import time
from pathlib import Path
from typing import List, Optional, Union

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from app.api.validators.image import (
    normalize_image_response_format,
    resolve_aspect_ratio,
    response_field_name,
    validate_image_edit_model,
    validate_image_generation_model,
    validate_image_request_common,
)
from app.core.config import get_config
from app.core.exceptions import AppException, ErrorType, ValidationException
from app.services.grok.services.image import ImageGenerationService
from app.services.grok.services.image_edit import ImageEditService
from app.services.grok.services.model import ModelService
from app.services.token import get_token_manager

router = APIRouter(tags=["Images"])


class ImageGenerationRequest(BaseModel):
    """图片生成请求 - OpenAI 兼容"""

    prompt: str = Field(..., description="图片描述")
    model: Optional[str] = Field("grok-imagine-1.0", description="模型名称")
    n: Optional[int] = Field(1, ge=1, le=10, description="生成数量 (1-10)")
    size: Optional[str] = Field(
        "1024x1024",
        description="图片尺寸: 1280x720, 720x1280, 1792x1024, 1024x1792, 1024x1024",
    )
    quality: Optional[str] = Field("standard", description="图片质量 (暂不支持)")
    response_format: Optional[str] = Field(None, description="响应格式")
    style: Optional[str] = Field(None, description="风格 (暂不支持)")
    stream: Optional[bool] = Field(False, description="是否流式输出")


class ImageEditRequest(BaseModel):
    """图片编辑请求 - OpenAI 兼容"""

    prompt: str = Field(..., description="编辑描述")
    model: Optional[str] = Field("grok-imagine-1.0-edit", description="模型名称")
    image: Optional[Union[str, List[str]]] = Field(None, description="待编辑图片文件")
    n: Optional[int] = Field(1, ge=1, le=10, description="生成数量 (1-10)")
    size: Optional[str] = Field(
        "1024x1024",
        description="图片尺寸: 1280x720, 720x1280, 1792x1024, 1024x1792, 1024x1024",
    )
    quality: Optional[str] = Field("standard", description="图片质量 (暂不支持)")
    response_format: Optional[str] = Field(None, description="响应格式")
    style: Optional[str] = Field(None, description="风格 (暂不支持)")
    stream: Optional[bool] = Field(False, description="是否流式输出")


def validate_generation_request(request: ImageGenerationRequest) -> None:
    model = request.model or "grok-imagine-1.0"
    validate_image_generation_model(model)
    validate_image_request_common(
        prompt=request.prompt,
        n=int(request.n or 1),
        stream=bool(request.stream),
        response_format=request.response_format,
        size=request.size,
        allow_ws_stream=True,
        n_param="n",
        stream_n_param="stream",
        response_format_param="response_format",
        size_param="size",
    )


def validate_edit_request(request: ImageEditRequest, images: List[UploadFile]) -> None:
    model = request.model or "grok-imagine-1.0-edit"
    validate_image_edit_model(model)
    validate_image_request_common(
        prompt=request.prompt,
        n=int(request.n or 1),
        stream=bool(request.stream),
        response_format=request.response_format,
        size=request.size,
        allow_ws_stream=False,
        n_param="n",
        stream_n_param="stream",
        response_format_param="response_format",
        size_param="size",
    )
    if not images:
        raise ValidationException(
            message="Image is required",
            param="image",
            code="missing_image",
        )
    if len(images) > 16:
        raise ValidationException(
            message="Too many images. Maximum is 16.",
            param="image",
            code="invalid_image_count",
        )


async def _get_token(model: str):
    token_mgr = await get_token_manager()
    await token_mgr.reload_if_stale()

    token = None
    for pool_name in ModelService.pool_candidates_for_model(model):
        token = token_mgr.get_token(pool_name)
        if token:
            break

    if not token:
        raise AppException(
            message="No available tokens. Please try again later.",
            error_type=ErrorType.RATE_LIMIT.value,
            code="rate_limit_exceeded",
            status_code=429,
        )

    return token_mgr, token


@router.post("/images/generations")
async def create_image(request: ImageGenerationRequest):
    if request.stream is None:
        request.stream = False
    if request.response_format is None:
        request.response_format = normalize_image_response_format(
            None,
            default_format=get_config("app.image_format"),
        )

    validate_generation_request(request)
    response_format = normalize_image_response_format(
        request.response_format,
        default_format=get_config("app.image_format"),
    )
    request.response_format = response_format
    response_field = response_field_name(response_format)

    token_mgr, token = await _get_token(request.model or "grok-imagine-1.0")
    model_info = ModelService.get(request.model)
    size = request.size or "1024x1024"
    aspect_ratio = resolve_aspect_ratio(size)

    result = await ImageGenerationService().generate(
        token_mgr=token_mgr,
        token=token,
        model_info=model_info,
        prompt=request.prompt,
        n=int(request.n or 1),
        response_format=response_format,
        size=size,
        aspect_ratio=aspect_ratio,
        stream=bool(request.stream),
    )

    if result.stream:
        return StreamingResponse(
            result.data,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    data = [{response_field: img} for img in result.data]
    usage = result.usage_override or {
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
    }

    return JSONResponse(
        content={
            "created": int(time.time()),
            "data": data,
            "usage": usage,
        }
    )


@router.post("/images/edits")
async def edit_image(
    prompt: str = Form(...),
    image: List[UploadFile] = File(...),
    model: Optional[str] = Form("grok-imagine-1.0-edit"),
    n: int = Form(1),
    size: str = Form("1024x1024"),
    quality: str = Form("standard"),
    response_format: Optional[str] = Form(None),
    style: Optional[str] = Form(None),
    stream: Optional[bool] = Form(False),
):
    if response_format is None:
        response_format = normalize_image_response_format(
            None,
            default_format=get_config("app.image_format"),
        )

    try:
        edit_request = ImageEditRequest(
            prompt=prompt,
            model=model,
            n=n,
            size=size,
            quality=quality,
            response_format=response_format,
            style=style,
            stream=stream,
        )
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = first.get("loc", [])
            msg = first.get("msg", "Invalid request")
            code = first.get("type", "invalid_value")
            param_parts = [
                str(x) for x in loc if not (isinstance(x, int) or str(x).isdigit())
            ]
            param = ".".join(param_parts) if param_parts else None
            raise ValidationException(message=msg, param=param, code=code)
        raise ValidationException(message="Invalid request", code="invalid_value")

    if edit_request.stream is None:
        edit_request.stream = False

    normalized_format = normalize_image_response_format(
        edit_request.response_format,
        default_format=get_config("app.image_format"),
    )
    edit_request.response_format = normalized_format
    response_field = response_field_name(normalized_format)
    validate_edit_request(edit_request, image)

    max_image_bytes = 50 * 1024 * 1024
    allowed_types = {"image/png", "image/jpeg", "image/webp", "image/jpg"}

    images: List[str] = []
    for item in image:
        content = await item.read()
        await item.close()
        if not content:
            raise ValidationException(
                message="File content is empty",
                param="image",
                code="empty_file",
            )
        if len(content) > max_image_bytes:
            raise ValidationException(
                message="Image file too large. Maximum is 50MB.",
                param="image",
                code="file_too_large",
            )
        mime = (item.content_type or "").lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        ext = Path(item.filename or "").suffix.lower()
        if mime not in allowed_types:
            if ext in (".jpg", ".jpeg"):
                mime = "image/jpeg"
            elif ext == ".png":
                mime = "image/png"
            elif ext == ".webp":
                mime = "image/webp"
            else:
                raise ValidationException(
                    message="Unsupported image type. Supported: png, jpg, webp.",
                    param="image",
                    code="invalid_image_type",
                )
        b64 = base64.b64encode(content).decode()
        images.append(f"data:{mime};base64,{b64}")

    token_mgr, token = await _get_token(edit_request.model or "grok-imagine-1.0-edit")
    model_info = ModelService.get(edit_request.model)

    result = await ImageEditService().edit(
        token_mgr=token_mgr,
        token=token,
        model_info=model_info,
        prompt=edit_request.prompt,
        images=images,
        n=int(edit_request.n or 1),
        response_format=normalized_format,
        stream=bool(edit_request.stream),
    )

    if result.stream:
        return StreamingResponse(
            result.data,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    data = [{response_field: img} for img in result.data]
    return JSONResponse(
        content={
            "created": int(time.time()),
            "data": data,
            "usage": {
                "total_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "input_tokens_details": {"text_tokens": 0, "image_tokens": 0},
            },
        }
    )


__all__ = ["router"]


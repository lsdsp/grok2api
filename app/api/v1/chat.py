"""
Chat Completions API 路由
"""

from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.api.validators.chat import (
    extract_prompt_images,
    validate_chat_completion_request,
)
from app.api.validators.image import resolve_aspect_ratio
from app.core.config import get_config
from app.core.exceptions import AppException, ErrorType, ValidationException
from app.services.grok.services.chat import ChatService
from app.services.grok.services.image import ImageGenerationService
from app.services.grok.services.image_edit import ImageEditService
from app.services.grok.services.model import ModelService
from app.services.grok.services.video import VideoService
from app.services.grok.utils.response import make_chat_response
from app.services.token import get_token_manager


class MessageItem(BaseModel):
    """消息项"""

    role: str
    content: Optional[Union[str, Dict[str, Any], List[Dict[str, Any]]]]
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class VideoConfig(BaseModel):
    """视频生成配置"""

    aspect_ratio: Optional[str] = Field(
        "3:2",
        description="视频比例: 1280x720(16:9), 720x1280(9:16), 1792x1024(3:2), 1024x1792(2:3), 1024x1024(1:1)",
    )
    video_length: Optional[int] = Field(6, description="视频时长(秒): 6 / 10 / 15")
    resolution_name: Optional[str] = Field("480p", description="视频分辨率: 480p, 720p")
    preset: Optional[str] = Field("custom", description="风格预设: fun, normal, spicy")


class ImageConfig(BaseModel):
    """图片生成配置"""

    n: Optional[int] = Field(1, ge=1, le=10, description="生成数量 (1-10)")
    size: Optional[str] = Field("1024x1024", description="图片尺寸")
    response_format: Optional[str] = Field(None, description="响应格式")


class ChatCompletionRequest(BaseModel):
    """Chat Completions 请求"""

    model: str = Field(..., description="模型名称")
    messages: List[MessageItem] = Field(..., description="消息数组")
    stream: Optional[bool] = Field(None, description="是否流式输出")
    reasoning_effort: Optional[str] = Field(
        None,
        description="推理强度: none/minimal/low/medium/high/xhigh",
    )
    temperature: Optional[float] = Field(0.8, description="采样温度: 0-2")
    top_p: Optional[float] = Field(0.95, description="nucleus 采样: 0-1")
    video_config: Optional[VideoConfig] = Field(None, description="视频生成参数")
    image_config: Optional[ImageConfig] = Field(None, description="图片生成参数")
    tools: Optional[List[Dict[str, Any]]] = Field(None, description="Tool definitions")
    tool_choice: Optional[Union[str, Dict[str, Any]]] = Field(
        None,
        description="Tool choice: auto/required/none/specific",
    )
    parallel_tool_calls: Optional[bool] = Field(
        True,
        description="Allow parallel tool calls",
    )


router = APIRouter(tags=["Chat"])


@router.post("/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    """Chat Completions API - 兼容 OpenAI"""
    from app.core.logger import logger

    validate_chat_completion_request(
        request,
        model_service=ModelService,
        image_config_factory=ImageConfig,
        video_config_factory=VideoConfig,
        default_image_format=get_config("app.image_format"),
        default_stream=bool(get_config("app.stream")),
    )

    logger.debug(f"Chat request: model={request.model}, stream={request.stream}")
    model_info = ModelService.get(request.model)

    if model_info and model_info.is_image_edit:
        prompt, image_urls = extract_prompt_images(request.messages)
        if not image_urls:
            raise ValidationException(
                message="Image is required",
                param="image",
                code="missing_image",
            )

        is_stream = request.stream if request.stream is not None else get_config("app.stream")
        image_conf = request.image_config or ImageConfig()
        response_format = image_conf.response_format or "b64_json"
        n = int(image_conf.n or 1)

        token_mgr = await get_token_manager()
        await token_mgr.reload_if_stale()

        token = None
        for pool_name in ModelService.pool_candidates_for_model(request.model):
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

        result = await ImageEditService().edit(
            token_mgr=token_mgr,
            token=token,
            model_info=model_info,
            prompt=prompt,
            images=image_urls,
            n=n,
            response_format=response_format,
            stream=bool(is_stream),
            chat_format=True,
        )

        if result.stream:
            return StreamingResponse(
                result.data,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        content = result.data[0] if result.data else ""
        return JSONResponse(content=make_chat_response(request.model, content))

    if model_info and model_info.is_image:
        prompt, _ = extract_prompt_images(request.messages)
        is_stream = request.stream if request.stream is not None else get_config("app.stream")
        image_conf = request.image_config or ImageConfig()
        response_format = image_conf.response_format or "b64_json"
        n = int(image_conf.n or 1)
        size = image_conf.size or "1024x1024"
        aspect_ratio = resolve_aspect_ratio(size)

        token_mgr = await get_token_manager()
        await token_mgr.reload_if_stale()

        token = None
        for pool_name in ModelService.pool_candidates_for_model(request.model):
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

        result = await ImageGenerationService().generate(
            token_mgr=token_mgr,
            token=token,
            model_info=model_info,
            prompt=prompt,
            n=n,
            response_format=response_format,
            size=size,
            aspect_ratio=aspect_ratio,
            stream=bool(is_stream),
            chat_format=True,
        )

        if result.stream:
            return StreamingResponse(
                result.data,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )

        content = result.data[0] if result.data else ""
        usage = result.usage_override
        return JSONResponse(content=make_chat_response(request.model, content, usage=usage))

    if model_info and model_info.is_video:
        v_conf = request.video_config or VideoConfig()
        result = await VideoService.completions(
            model=request.model,
            messages=[msg.model_dump() for msg in request.messages],
            stream=request.stream,
            reasoning_effort=request.reasoning_effort,
            aspect_ratio=v_conf.aspect_ratio,
            video_length=v_conf.video_length,
            resolution=v_conf.resolution_name,
            preset=v_conf.preset,
        )
    else:
        result = await ChatService.completions(
            model=request.model,
            messages=[msg.model_dump() for msg in request.messages],
            stream=request.stream,
            reasoning_effort=request.reasoning_effort,
            temperature=request.temperature,
            top_p=request.top_p,
            tools=request.tools,
            tool_choice=request.tool_choice,
            parallel_tool_calls=request.parallel_tool_calls,
        )

    if isinstance(result, dict):
        return JSONResponse(content=result)
    return StreamingResponse(
        result,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


__all__ = ["router"]


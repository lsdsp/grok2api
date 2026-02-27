"""
Chat 请求校验与规范化
"""

import base64
import binascii
from typing import Any, List, Optional

from app.core.exceptions import ValidationException
from app.api.validators.image import (
    validate_image_request_common,
    normalize_image_response_format,
)

VALID_ROLES = {"developer", "system", "user", "assistant", "tool"}
USER_CONTENT_TYPES = {"text", "image_url", "input_audio", "file"}

VIDEO_ASPECT_RATIO_MAP = {
    "1280x720": "16:9",
    "720x1280": "9:16",
    "1792x1024": "3:2",
    "1024x1792": "2:3",
    "1024x1024": "1:1",
    "16:9": "16:9",
    "9:16": "9:16",
    "3:2": "3:2",
    "2:3": "2:3",
    "1:1": "1:1",
}


def _validate_media_input(value: str, field_name: str, param: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationException(
            message=f"{field_name} cannot be empty",
            param=param,
            code="empty_media",
        )
    raw = value.strip()
    if raw.startswith("data:"):
        return
    if raw.startswith("http://") or raw.startswith("https://"):
        return
    candidate = "".join(raw.split())
    if len(candidate) >= 32 and len(candidate) % 4 == 0:
        try:
            base64.b64decode(candidate, validate=True)
            raise ValidationException(
                message=f"{field_name} base64 must be provided as a data URI (data:<mime>;base64,...)",
                param=param,
                code="invalid_media",
            )
        except binascii.Error:
            pass
    raise ValidationException(
        message=f"{field_name} must be a URL or data URI",
        param=param,
        code="invalid_media",
    )


def extract_prompt_images(messages: List[Any]) -> tuple[str, List[str]]:
    last_text = ""
    image_urls: List[str] = []

    for msg in messages:
        role = getattr(msg, "role", None) or "user"
        content = getattr(msg, "content", None)
        if isinstance(content, str):
            text = content.strip()
            if text:
                last_text = text
            continue
        if isinstance(content, dict):
            content = [content]
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text.strip():
                    last_text = text.strip()
            elif block_type == "image_url" and role == "user":
                image = block.get("image_url") or {}
                url = image.get("url", "")
                if isinstance(url, str) and url.strip():
                    image_urls.append(url.strip())

    return last_text, image_urls


def _validate_image_config(image_conf: Any, *, stream: bool) -> None:
    validate_image_request_common(
        prompt="dummy",
        n=int(getattr(image_conf, "n", 1) or 1),
        stream=stream,
        response_format=getattr(image_conf, "response_format", None),
        size=getattr(image_conf, "size", None),
        allow_ws_stream=True,
        n_param="image_config.n",
        stream_n_param="image_config.n",
        response_format_param="image_config.response_format",
        size_param="image_config.size",
    )


def _validate_messages(messages: List[Any]) -> None:
    for idx, msg in enumerate(messages):
        role = getattr(msg, "role", None)
        if not isinstance(role, str) or role not in VALID_ROLES:
            raise ValidationException(
                message=f"role must be one of {sorted(VALID_ROLES)}",
                param=f"messages.{idx}.role",
                code="invalid_role",
            )

        tool_call_id = getattr(msg, "tool_call_id", None)
        tool_calls = getattr(msg, "tool_calls", None)
        if role == "tool":
            if not tool_call_id:
                raise ValidationException(
                    message="tool messages must have a 'tool_call_id' field",
                    param=f"messages.{idx}.tool_call_id",
                    code="missing_tool_call_id",
                )
            continue

        if role == "assistant" and tool_calls:
            continue

        content = getattr(msg, "content", None)
        if content is None:
            if role in {"assistant", "tool"}:
                continue
            raise ValidationException(
                message="Message content cannot be null",
                param=f"messages.{idx}.content",
                code="empty_content",
            )

        if isinstance(content, str):
            if not content.strip():
                raise ValidationException(
                    message="Message content cannot be empty",
                    param=f"messages.{idx}.content",
                    code="empty_content",
                )
        elif isinstance(content, dict):
            items = [content]
            for c_idx, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValidationException(
                        message="Message content items must be objects",
                        param=f"messages.{idx}.content.{c_idx}",
                        code="invalid_content_item",
                    )
                item_type = item.get("type")
                if item_type != "text":
                    raise ValidationException(
                        message="When content is an object, type must be 'text'",
                        param=f"messages.{idx}.content.{c_idx}.type",
                        code="invalid_content_type",
                    )
                text = item.get("text", "")
                if not isinstance(text, str) or not text.strip():
                    raise ValidationException(
                        message=f"messages.{idx}.content.{c_idx}.text must be a non-empty string",
                        param=f"messages.{idx}.content.{c_idx}.text",
                        code="empty_content",
                    )
        elif isinstance(content, list):
            if not content:
                raise ValidationException(
                    message="Message content cannot be an empty array",
                    param=f"messages.{idx}.content",
                    code="empty_content",
                )

            for block_idx, block in enumerate(content):
                if not isinstance(block, dict):
                    raise ValidationException(
                        message="Content block must be an object",
                        param=f"messages.{idx}.content.{block_idx}",
                        code="invalid_block",
                    )
                if not block:
                    raise ValidationException(
                        message="Content block cannot be empty",
                        param=f"messages.{idx}.content.{block_idx}",
                        code="empty_block",
                    )
                if "type" not in block:
                    raise ValidationException(
                        message="Content block must have a 'type' field",
                        param=f"messages.{idx}.content.{block_idx}",
                        code="missing_type",
                    )

                block_type = block.get("type")
                if not block_type or not isinstance(block_type, str) or not block_type.strip():
                    raise ValidationException(
                        message="Content block 'type' cannot be empty",
                        param=f"messages.{idx}.content.{block_idx}.type",
                        code="empty_type",
                    )

                if role == "user":
                    if block_type not in USER_CONTENT_TYPES:
                        raise ValidationException(
                            message=f"Invalid content block type: '{block_type}'",
                            param=f"messages.{idx}.content.{block_idx}.type",
                            code="invalid_type",
                        )
                elif block_type != "text":
                    raise ValidationException(
                        message=f"The `{role}` role only supports 'text' type, got '{block_type}'",
                        param=f"messages.{idx}.content.{block_idx}.type",
                        code="invalid_type",
                    )

                if block_type == "text":
                    text = block.get("text", "")
                    if not isinstance(text, str) or not text.strip():
                        raise ValidationException(
                            message="Text content cannot be empty",
                            param=f"messages.{idx}.content.{block_idx}.text",
                            code="empty_text",
                        )
                elif block_type == "image_url":
                    image_url = block.get("image_url")
                    if not image_url or not isinstance(image_url, dict):
                        raise ValidationException(
                            message="image_url must have a 'url' field",
                            param=f"messages.{idx}.content.{block_idx}.image_url",
                            code="missing_url",
                        )
                    _validate_media_input(
                        image_url.get("url", ""),
                        "image_url.url",
                        f"messages.{idx}.content.{block_idx}.image_url.url",
                    )
                elif block_type == "input_audio":
                    audio = block.get("input_audio")
                    if not audio or not isinstance(audio, dict):
                        raise ValidationException(
                            message="input_audio must have a 'data' field",
                            param=f"messages.{idx}.content.{block_idx}.input_audio",
                            code="missing_audio",
                        )
                    _validate_media_input(
                        audio.get("data", ""),
                        "input_audio.data",
                        f"messages.{idx}.content.{block_idx}.input_audio.data",
                    )
                elif block_type == "file":
                    file_data = block.get("file")
                    if not file_data or not isinstance(file_data, dict):
                        raise ValidationException(
                            message="file must have a 'file_data' field",
                            param=f"messages.{idx}.content.{block_idx}.file",
                            code="missing_file",
                        )
                    _validate_media_input(
                        file_data.get("file_data", ""),
                        "file.file_data",
                        f"messages.{idx}.content.{block_idx}.file.file_data",
                    )
        else:
            raise ValidationException(
                message="Message content must be a string or array",
                param=f"messages.{idx}.content",
                code="invalid_content",
            )


def validate_chat_completion_request(
    request: Any,
    *,
    model_service: Any,
    image_config_factory: Any,
    video_config_factory: Any,
    default_image_format: Optional[str],
    default_stream: bool,
) -> None:
    if not model_service.valid(request.model):
        raise ValidationException(
            message=f"The model `{request.model}` does not exist or you do not have access to it.",
            param="model",
            code="model_not_found",
        )

    _validate_messages(request.messages)

    if request.stream is not None:
        if isinstance(request.stream, bool):
            pass
        elif isinstance(request.stream, str):
            lowered = request.stream.lower()
            if lowered in ("true", "1", "yes"):
                request.stream = True
            elif lowered in ("false", "0", "no"):
                request.stream = False
            else:
                raise ValidationException(
                    message="stream must be a boolean",
                    param="stream",
                    code="invalid_stream",
                )
        else:
            raise ValidationException(
                message="stream must be a boolean",
                param="stream",
                code="invalid_stream",
            )

    allowed_efforts = {"none", "minimal", "low", "medium", "high", "xhigh"}
    if request.reasoning_effort is not None:
        if not isinstance(request.reasoning_effort, str) or request.reasoning_effort not in allowed_efforts:
            raise ValidationException(
                message=f"reasoning_effort must be one of {sorted(allowed_efforts)}",
                param="reasoning_effort",
                code="invalid_reasoning_effort",
            )

    if request.temperature is None:
        request.temperature = 0.8
    else:
        try:
            request.temperature = float(request.temperature)
        except Exception:
            raise ValidationException(
                message="temperature must be a float",
                param="temperature",
                code="invalid_temperature",
            )
        if not (0 <= request.temperature <= 2):
            raise ValidationException(
                message="temperature must be between 0 and 2",
                param="temperature",
                code="invalid_temperature",
            )

    if request.top_p is None:
        request.top_p = 0.95
    else:
        try:
            request.top_p = float(request.top_p)
        except Exception:
            raise ValidationException(
                message="top_p must be a float",
                param="top_p",
                code="invalid_top_p",
            )
        if not (0 <= request.top_p <= 1):
            raise ValidationException(
                message="top_p must be between 0 and 1",
                param="top_p",
                code="invalid_top_p",
            )

    if request.tools is not None:
        if not isinstance(request.tools, list):
            raise ValidationException(
                message="tools must be an array",
                param="tools",
                code="invalid_tools",
            )
        for t_idx, tool in enumerate(request.tools):
            if not isinstance(tool, dict) or tool.get("type") != "function":
                raise ValidationException(
                    message="Each tool must have type='function'",
                    param=f"tools.{t_idx}.type",
                    code="invalid_tool_type",
                )
            func = tool.get("function")
            if not isinstance(func, dict) or not func.get("name"):
                raise ValidationException(
                    message="Each tool function must have a 'name'",
                    param=f"tools.{t_idx}.function.name",
                    code="missing_function_name",
                )

    if request.tool_choice is not None:
        if isinstance(request.tool_choice, str):
            if request.tool_choice not in ("auto", "required", "none"):
                raise ValidationException(
                    message="tool_choice must be 'auto', 'required', 'none', or a specific function object",
                    param="tool_choice",
                    code="invalid_tool_choice",
                )
        elif isinstance(request.tool_choice, dict):
            if request.tool_choice.get("type") != "function" or not request.tool_choice.get("function", {}).get("name"):
                raise ValidationException(
                    message="tool_choice object must have type='function' and function.name",
                    param="tool_choice",
                    code="invalid_tool_choice",
                )

    model_info = model_service.get(request.model)
    effective_stream = request.stream if request.stream is not None else default_stream

    if model_info and (model_info.is_image or model_info.is_image_edit):
        prompt, _ = extract_prompt_images(request.messages)
        if not prompt:
            raise ValidationException(
                message="Prompt cannot be empty",
                param="messages",
                code="empty_prompt",
            )
        image_conf = request.image_config or image_config_factory()
        _validate_image_config(image_conf, stream=bool(effective_stream))
        image_conf.n = int(getattr(image_conf, "n", 1) or 1)
        image_conf.response_format = normalize_image_response_format(
            getattr(image_conf, "response_format", None),
            default_format=default_image_format,
            message="image_format must be one of url, base64, b64_json",
            param="image_format",
            code="invalid_image_format",
        )
        if not getattr(image_conf, "size", None):
            image_conf.size = "1024x1024"
        request.image_config = image_conf

    if model_info and model_info.is_image_edit:
        _, image_urls = extract_prompt_images(request.messages)
        if not image_urls:
            raise ValidationException(
                message="image_url is required for image edits",
                param="messages",
                code="missing_image",
            )

    if model_info and model_info.is_video:
        config = request.video_config or video_config_factory()
        if getattr(config, "aspect_ratio", None) is None:
            config.aspect_ratio = "3:2"
        if config.aspect_ratio not in VIDEO_ASPECT_RATIO_MAP:
            raise ValidationException(
                message=f"aspect_ratio must be one of {list(VIDEO_ASPECT_RATIO_MAP.keys())}",
                param="video_config.aspect_ratio",
                code="invalid_aspect_ratio",
            )
        config.aspect_ratio = VIDEO_ASPECT_RATIO_MAP[config.aspect_ratio]

        if config.video_length not in (6, 10, 15):
            raise ValidationException(
                message="video_length must be 6, 10, or 15 seconds",
                param="video_config.video_length",
                code="invalid_video_length",
            )
        if config.resolution_name not in ("480p", "720p"):
            raise ValidationException(
                message="resolution_name must be one of ['480p', '720p']",
                param="video_config.resolution_name",
                code="invalid_resolution",
            )
        if config.preset not in ("fun", "normal", "spicy", "custom"):
            raise ValidationException(
                message="preset must be one of ['fun', 'normal', 'spicy', 'custom']",
                param="video_config.preset",
                code="invalid_preset",
            )
        request.video_config = config


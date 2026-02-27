"""
图像请求校验与规范化
"""

from typing import Optional

from app.core.exceptions import ValidationException
from app.services.grok.services.model import ModelService

ALLOWED_IMAGE_SIZES = {
    "1280x720",
    "720x1280",
    "1792x1024",
    "1024x1792",
    "1024x1024",
}

SIZE_TO_ASPECT = {
    "1280x720": "16:9",
    "720x1280": "9:16",
    "1792x1024": "3:2",
    "1024x1792": "2:3",
    "1024x1024": "1:1",
}

ALLOWED_ASPECT_RATIOS = {"1:1", "2:3", "3:2", "9:16", "16:9"}
ALLOWED_RESPONSE_FORMATS = {"b64_json", "base64", "url"}


def normalize_image_response_format(
    response_format: Optional[str],
    *,
    default_format: Optional[str] = "url",
    message: str = "response_format must be one of b64_json, base64, url",
    param: str = "response_format",
    code: str = "invalid_response_format",
) -> str:
    fmt = response_format if response_format is not None else default_format
    if isinstance(fmt, str):
        fmt = fmt.strip().lower()
    if fmt == "base64":
        return "b64_json"
    if fmt in {"b64_json", "url"}:
        return fmt
    raise ValidationException(message=message, param=param, code=code)


def response_field_name(response_format: str) -> str:
    return "url" if response_format == "url" else "b64_json"


def validate_image_request_common(
    *,
    prompt: str,
    n: int,
    stream: bool,
    response_format: Optional[str],
    size: Optional[str],
    allow_ws_stream: bool,
    n_param: str,
    stream_n_param: str,
    response_format_param: str,
    size_param: str,
) -> None:
    if not prompt or not prompt.strip():
        raise ValidationException(
            message="Prompt cannot be empty",
            param="prompt",
            code="empty_prompt",
        )

    if n < 1 or n > 10:
        raise ValidationException(
            message="n must be between 1 and 10",
            param=n_param,
            code="invalid_n",
        )

    if stream and n not in (1, 2):
        raise ValidationException(
            message="Streaming is only supported when n=1 or n=2",
            param=stream_n_param,
            code="invalid_stream_n",
        )

    if response_format:
        value = response_format.strip().lower()
        if value not in ALLOWED_RESPONSE_FORMATS:
            raise ValidationException(
                message="response_format must be one of b64_json, base64, url",
                param=response_format_param,
                code="invalid_response_format",
            )
        if allow_ws_stream and stream and value not in ALLOWED_RESPONSE_FORMATS:
            raise ValidationException(
                message="Streaming only supports response_format=b64_json/base64/url",
                param=response_format_param,
                code="invalid_response_format",
            )

    if size and size not in ALLOWED_IMAGE_SIZES:
        raise ValidationException(
            message=f"size must be one of {sorted(ALLOWED_IMAGE_SIZES)}",
            param=size_param,
            code="invalid_size",
        )


def validate_image_generation_model(model: str) -> None:
    if model != "grok-imagine-1.0":
        raise ValidationException(
            message="The model `grok-imagine-1.0` is required for image generation.",
            param="model",
            code="model_not_supported",
        )
    model_info = ModelService.get(model)
    if not model_info or not model_info.is_image:
        image_models = [m.model_id for m in ModelService.MODELS if m.is_image]
        raise ValidationException(
            message=(
                f"The model `{model}` is not supported for image generation. "
                f"Supported: {image_models}"
            ),
            param="model",
            code="model_not_supported",
        )


def validate_image_edit_model(model: str) -> None:
    if model != "grok-imagine-1.0-edit":
        raise ValidationException(
            message="The model `grok-imagine-1.0-edit` is required for image edits.",
            param="model",
            code="model_not_supported",
        )
    model_info = ModelService.get(model)
    if not model_info or not model_info.is_image_edit:
        edit_models = [m.model_id for m in ModelService.MODELS if m.is_image_edit]
        raise ValidationException(
            message=(
                f"The model `{model}` is not supported for image edits. "
                f"Supported: {edit_models}"
            ),
            param="model",
            code="model_not_supported",
        )


def resolve_aspect_ratio(size: str) -> str:
    value = (size or "").strip()
    if not value:
        return "2:3"
    if value in SIZE_TO_ASPECT:
        return SIZE_TO_ASPECT[value]
    if ":" in value:
        try:
            left, right = value.split(":", 1)
            left_i = int(left.strip())
            right_i = int(right.strip())
            if left_i > 0 and right_i > 0:
                ratio = f"{left_i}:{right_i}"
                if ratio in ALLOWED_ASPECT_RATIOS:
                    return ratio
        except (TypeError, ValueError):
            pass
    return "2:3"


"""
响应中间件
Response Middleware

用于记录请求日志、生成 TraceID 和计算请求耗时
"""

import time
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.config import get_config
from app.core.logger import logger

DEFAULT_IGNORE_PATHS = [
    "/",
    "/login",
    "/imagine",
    "/voice",
    "/admin",
    "/admin/login",
    "/admin/config",
    "/admin/cache",
    "/admin/token",
]
DEFAULT_IGNORE_PREFIXES = ["/static/"]


def _as_list(value, default):
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or list(default)
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
        return items or list(default)
    return list(default)


def _should_skip_logging(path: str) -> bool:
    ignore_paths_raw = get_config("logging.ignore_paths", DEFAULT_IGNORE_PATHS)
    ignore_paths = set(_as_list(ignore_paths_raw, DEFAULT_IGNORE_PATHS))
    ignore_prefixes = _as_list(
        get_config("logging.ignore_prefixes", DEFAULT_IGNORE_PREFIXES),
        DEFAULT_IGNORE_PREFIXES,
    )
    if path in ignore_paths:
        return True
    return any(path.startswith(prefix) for prefix in ignore_prefixes)


class ResponseLoggerMiddleware(BaseHTTPMiddleware):
    """
    请求日志/响应追踪中间件
    Request Logging and Response Tracking Middleware
    """

    async def dispatch(self, request: Request, call_next):
        # 生成请求 ID
        trace_id = str(uuid.uuid4())
        request.state.trace_id = trace_id

        start_time = time.time()
        path = request.url.path
        request_logger = logger.bind(
            traceID=trace_id,
            method=request.method,
            path=path,
        )

        if _should_skip_logging(path):
            response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            return response

        # 记录请求信息
        request_logger.info(f"Request: {request.method} {path}")

        try:
            response = await call_next(request)

            # 计算耗时
            duration = (time.time() - start_time) * 1000
            response.headers["X-Trace-Id"] = trace_id

            # 记录响应信息
            request_logger.bind(
                status=response.status_code,
                duration_ms=round(duration, 2),
            ).info(
                f"Response: {request.method} {path} - {response.status_code} ({duration:.2f}ms)"
            )

            return response

        except Exception as e:
            duration = (time.time() - start_time) * 1000
            request_logger.bind(
                duration_ms=round(duration, 2),
                error=str(e),
            ).error(
                f"Response Error: {request.method} {path} - {str(e)} ({duration:.2f}ms)"
            )
            raise e

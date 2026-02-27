"""
入口级请求限流中间件
"""

import asyncio
import hashlib
import math
import time
from collections import deque
from typing import Deque, Dict, Tuple

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core.auth import is_production_env
from app.core.config import get_config
from app.core.exceptions import error_response, ErrorType
from app.core.logger import logger

DEFAULT_INCLUDE_PREFIXES = ["/v1/"]
DEFAULT_EXCLUDE_PREFIXES = ["/v1/admin", "/v1/files", "/static/"]


def _as_list(value, default):
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or list(default)
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
        return items or list(default)
    return list(default)


def _to_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _extract_ip(request: Request, trust_x_forwarded_for: bool) -> str:
    if trust_x_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            first = forwarded.split(",")[0].strip()
            if first:
                return first
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _extract_client_key(request: Request, trust_x_forwarded_for: bool) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
            return f"ak:{digest}"
    return f"ip:{_extract_ip(request, trust_x_forwarded_for)}"


def _resolve_path_limit(path: str, route_limits: dict, default_limit: int) -> int:
    if not isinstance(route_limits, dict):
        return default_limit

    exact = route_limits.get(path)
    if exact is not None:
        return max(0, _to_int(exact, default_limit))

    best = None
    best_len = -1
    for pattern, value in route_limits.items():
        if not isinstance(pattern, str) or not pattern.endswith("*"):
            continue
        prefix = pattern[:-1]
        if path.startswith(prefix) and len(prefix) > best_len:
            best_len = len(prefix)
            best = value

    if best is None:
        return default_limit
    return max(0, _to_int(best, default_limit))


class _SlidingWindowLimiter:
    def __init__(self):
        self._buckets: Dict[str, Deque[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, key: str, limit: int, window_seconds: float) -> Tuple[bool, float]:
        now = time.monotonic()
        cutoff = now - window_seconds

        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= limit:
                retry_after = max(0.0, window_seconds - (now - bucket[0]))
                return False, retry_after

            bucket.append(now)

            # 惰性清理，避免 key 无限增长
            if len(self._buckets) > 20000:
                stale_cutoff = now - (window_seconds * 2)
                stale_keys = [
                    bucket_key
                    for bucket_key, times in self._buckets.items()
                    if not times or times[-1] < stale_cutoff
                ]
                for bucket_key in stale_keys:
                    self._buckets.pop(bucket_key, None)

            return True, 0.0


_LIMITER = _SlidingWindowLimiter()


class RequestRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        enabled = bool(get_config("rate_limit.enabled", False))
        if not enabled and is_production_env():
            enabled = bool(get_config("rate_limit.enabled_in_production", True))
        if not enabled:
            return await call_next(request)

        path = request.url.path
        include_prefixes = _as_list(
            get_config("rate_limit.include_prefixes", DEFAULT_INCLUDE_PREFIXES),
            DEFAULT_INCLUDE_PREFIXES,
        )
        exclude_prefixes = _as_list(
            get_config("rate_limit.exclude_prefixes", DEFAULT_EXCLUDE_PREFIXES),
            DEFAULT_EXCLUDE_PREFIXES,
        )

        if include_prefixes and not any(path.startswith(prefix) for prefix in include_prefixes):
            return await call_next(request)
        if exclude_prefixes and any(path.startswith(prefix) for prefix in exclude_prefixes):
            return await call_next(request)

        default_limit = max(1, _to_int(get_config("rate_limit.default_limit_per_window", 120), 120))
        window_seconds = max(1.0, _to_float(get_config("rate_limit.window_seconds", 60), 60.0))
        route_limits = get_config("rate_limit.route_limits", {})
        limit = _resolve_path_limit(path, route_limits, default_limit)
        if limit <= 0:
            return await call_next(request)

        trust_xff = bool(get_config("rate_limit.trust_x_forwarded_for", False))
        client_key = _extract_client_key(request, trust_xff)
        bucket_key = f"{request.method}:{path}:{client_key}"

        allowed, retry_after = await _LIMITER.allow(bucket_key, limit, window_seconds)
        if allowed:
            return await call_next(request)

        retry_after_seconds = max(1, int(math.ceil(retry_after)))
        trace_id = getattr(request.state, "trace_id", None)
        blocked_logger = logger.bind(
            method=request.method,
            path=path,
            client=client_key,
            limit=limit,
            window_seconds=window_seconds,
            retry_after=retry_after_seconds,
        )
        if trace_id:
            blocked_logger = blocked_logger.bind(traceID=trace_id)
        blocked_logger.warning("Request blocked by global rate limiter")

        headers = {"Retry-After": str(retry_after_seconds)}
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        return JSONResponse(
            status_code=429,
            headers=headers,
            content=error_response(
                message="Rate limit exceeded. Please retry later.",
                error_type=ErrorType.RATE_LIMIT.value,
                code="rate_limit_exceeded",
            ),
        )

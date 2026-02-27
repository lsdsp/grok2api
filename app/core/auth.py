"""
API 认证模块
"""

import os
from typing import Optional
from fastapi import HTTPException, status, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.core.config import get_config

DEFAULT_API_KEY = ""
DEFAULT_APP_KEY = "grok2api"
DEFAULT_PUBLIC_KEY = ""
DEFAULT_PUBLIC_ENABLED = False
DEFAULT_AUTH_REQUIRED = False
DEFAULT_FILES_PUBLIC = True

# 定义 Bearer Scheme
security = HTTPBearer(
    auto_error=False,
    scheme_name="API Key",
    description="Enter your API Key in the format: Bearer <key>",
)


def get_admin_api_key() -> str:
    """
    获取后台 API Key。

    为空时表示不启用后台接口认证。
    """
    api_key = get_config("app.api_key", DEFAULT_API_KEY)
    return api_key or ""


def get_app_key() -> str:
    """
    获取 App Key（后台管理密码）。
    """
    app_key = get_config("app.app_key", DEFAULT_APP_KEY)
    return app_key or ""


def get_public_api_key() -> str:
    """
    获取 Public API Key。

    为空时表示不启用 public 接口认证。
    """
    public_key = get_config("app.public_key", DEFAULT_PUBLIC_KEY)
    return public_key or ""


def is_public_enabled() -> bool:
    """
    是否开启 public 功能入口。
    """
    return bool(get_config("app.public_enabled", DEFAULT_PUBLIC_ENABLED))


def is_api_auth_required() -> bool:
    """
    是否启用 API Key 鉴权。
    """
    return bool(get_config("security.auth_required", DEFAULT_AUTH_REQUIRED))


def is_files_public() -> bool:
    """
    文件服务是否允许匿名访问。
    """
    return bool(get_config("security.files_public", DEFAULT_FILES_PUBLIC))


def is_production_env() -> bool:
    """
    检测当前是否生产环境。
    """
    env = (os.getenv("APP_ENV") or os.getenv("ENV") or "").strip().lower()
    return env in {"prod", "production"}


def _validate_bearer(
    auth: Optional[HTTPAuthorizationCredentials],
    expected: str,
    *,
    misconfigured_detail: str,
) -> str:
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=misconfigured_detail,
        )

    if not auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if auth.credentials != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return auth.credentials


async def verify_api_key(
    auth: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> Optional[str]:
    """
    验证 Bearer Token

    当 security.auth_required = false 时，跳过认证。
    当 security.auth_required = true 时，必须配置 app.api_key。
    """
    if not is_api_auth_required():
        return None

    api_key = get_admin_api_key()
    return _validate_bearer(
        auth,
        api_key,
        misconfigured_detail="API authentication is enabled but app.api_key is empty",
    )


async def verify_api_key_required(
    auth: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> str:
    """
    强制验证 API Key（不受 security.auth_required 影响）。
    """
    api_key = get_admin_api_key()
    return _validate_bearer(
        auth,
        api_key,
        misconfigured_detail="File access authentication is enabled but app.api_key is empty",
    )


async def verify_app_key(
    auth: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> Optional[str]:
    """
    验证后台登录密钥（app_key）。

    app_key 必须配置，否则拒绝登录。
    """
    app_key = get_app_key()

    if not app_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="App key is not configured",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if auth.credentials != app_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return auth.credentials


async def verify_public_key(
    auth: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> Optional[str]:
    """
    验证 Public Key（public 接口使用）。

    默认不公开，需配置 public_key 才能访问；若开启 public_enabled 且未配置 public_key，则放开访问。
    """
    public_key = get_public_api_key()
    public_enabled = is_public_enabled()

    if not public_key:
        if public_enabled:
            return None
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Public access is disabled",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if auth.credentials != public_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return auth.credentials

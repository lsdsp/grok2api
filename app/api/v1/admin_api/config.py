import os
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import verify_app_key
from app.core.config import config
from app.core.logger import logger
from app.core.storage import get_storage, LocalStorage, RedisStorage, SQLStorage
from app.services.token.manager import get_token_manager

router = APIRouter()


def _mask_secret(value: str, head: int = 8, tail: int = 8) -> str:
    if not value:
        return ""
    if len(value) <= head + tail:
        return value[:2] + "***" + value[-2:]
    return f"{value[:head]}...{value[-tail:]}"


SENSITIVE_PATHS = {
    ("app", "api_key"),
    ("app", "app_key"),
    ("app", "public_key"),
    ("proxy", "cf_clearance"),
}
SENSITIVE_KEY_HINTS = ("token", "secret", "password", "clearance")


def _is_sensitive_path(path: tuple[str, ...]) -> bool:
    if len(path) >= 2 and (path[-2], path[-1]) in SENSITIVE_PATHS:
        return True
    if not path:
        return False
    key = path[-1].lower()
    if key.endswith("_key"):
        return True
    return any(hint in key for hint in SENSITIVE_KEY_HINTS)


def _mask_config_values(data: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(data, dict):
        return {
            key: _mask_config_values(value, path + (str(key),))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [_mask_config_values(item, path) for item in data]
    if isinstance(data, str) and data and _is_sensitive_path(path):
        return _mask_secret(data)
    return data


def _restore_masked_secrets(
    incoming: Any, current: Any, path: tuple[str, ...] = ()
) -> Any:
    """
    如果前端提交的值仍是掩码，保留原始密文，避免被 "***" 覆盖。
    """
    if isinstance(incoming, dict):
        current_dict = current if isinstance(current, dict) else {}
        return {
            key: _restore_masked_secrets(
                value, current_dict.get(key), path + (str(key),)
            )
            for key, value in incoming.items()
        }
    if isinstance(incoming, list):
        return incoming
    if (
        isinstance(incoming, str)
        and isinstance(current, str)
        and current
        and _is_sensitive_path(path)
        and incoming == _mask_secret(current)
    ):
        return current
    return incoming


@router.get("/verify", dependencies=[Depends(verify_app_key)])
async def admin_verify():
    """验证后台访问密钥（app_key）"""
    return {"status": "success"}


@router.get("/config", dependencies=[Depends(verify_app_key)])
async def get_config():
    """获取当前配置"""
    return _mask_config_values(deepcopy(config._config))


@router.post("/config", dependencies=[Depends(verify_app_key)])
async def update_config(data: dict):
    """更新配置"""
    try:
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Invalid config payload")
        merged_input = _restore_masked_secrets(data, config._config)
        await config.update(merged_input)
        proxy_cfg = (
            merged_input.get("proxy") if isinstance(merged_input, dict) else None
        )
        if isinstance(proxy_cfg, dict) and "cf_clearance" in proxy_cfg:
            mgr = await get_token_manager()
            mgr.clear_auto_refresh_pause()
        return {"status": "success", "message": "配置已更新"}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to update config: {e}")
        raise HTTPException(status_code=500, detail="Failed to update config")


@router.post("/config/proxy/cf_clearance", dependencies=[Depends(verify_app_key)])
async def update_proxy_cf_clearance(data: dict):
    """快速更新 Cloudflare clearance 配置。"""
    try:
        cf_clearance = data.get("cf_clearance")
        if not isinstance(cf_clearance, str) or not cf_clearance.strip():
            raise HTTPException(status_code=400, detail="cf_clearance is required")

        patch = {
            "proxy": {
                "cf_clearance": cf_clearance.strip(),
            }
        }

        user_agent = data.get("user_agent")
        if isinstance(user_agent, str) and user_agent.strip():
            patch["proxy"]["user_agent"] = user_agent.strip()

        await config.update(patch)

        mgr = await get_token_manager()
        mgr.clear_auto_refresh_pause()

        return {
            "status": "success",
            "message": "cf_clearance 已更新并立即生效",
            "proxy": {
                "cf_clearance_masked": _mask_secret(patch["proxy"]["cf_clearance"]),
                "user_agent": config.get("proxy.user_agent"),
            },
            "refresh_pause": mgr.get_refresh_state(),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to update cf_clearance: {e}")
        raise HTTPException(status_code=500, detail="Failed to update cf_clearance")


@router.get("/storage", dependencies=[Depends(verify_app_key)])
async def get_storage():
    """获取当前存储模式"""
    storage_type = os.getenv("SERVER_STORAGE_TYPE", "").lower()
    if not storage_type:
        storage = get_storage()
        if isinstance(storage, LocalStorage):
            storage_type = "local"
        elif isinstance(storage, RedisStorage):
            storage_type = "redis"
        elif isinstance(storage, SQLStorage):
            storage_type = {
                "mysql": "mysql",
                "mariadb": "mysql",
                "postgres": "pgsql",
                "postgresql": "pgsql",
                "pgsql": "pgsql",
            }.get(storage.dialect, storage.dialect)
    return {"type": storage_type or "local"}

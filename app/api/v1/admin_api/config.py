import os

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import verify_app_key
from app.core.config import config
from app.core.storage import get_storage, LocalStorage, RedisStorage, SQLStorage
from app.services.token.manager import get_token_manager

router = APIRouter()


def _mask_secret(value: str, head: int = 8, tail: int = 8) -> str:
    if not value:
        return ""
    if len(value) <= head + tail:
        return value[:2] + "***" + value[-2:]
    return f"{value[:head]}...{value[-tail:]}"


@router.get("/verify", dependencies=[Depends(verify_app_key)])
async def admin_verify():
    """验证后台访问密钥（app_key）"""
    return {"status": "success"}


@router.get("/config", dependencies=[Depends(verify_app_key)])
async def get_config():
    """获取当前配置"""
    # 暴露原始配置字典
    return config._config


@router.post("/config", dependencies=[Depends(verify_app_key)])
async def update_config(data: dict):
    """更新配置"""
    try:
        await config.update(data)
        proxy_cfg = data.get("proxy") if isinstance(data, dict) else None
        if isinstance(proxy_cfg, dict) and "cf_clearance" in proxy_cfg:
            mgr = await get_token_manager()
            mgr.clear_auto_refresh_pause()
        return {"status": "success", "message": "配置已更新"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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

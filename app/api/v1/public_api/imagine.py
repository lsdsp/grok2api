import asyncio
import time
import uuid
from typing import Optional, List, Dict, Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.auth import verify_public_key, get_public_api_key, is_public_enabled
from app.core.config import get_config
from app.core.logger import logger
from app.api.v1.image import resolve_aspect_ratio
from app.services.grok.services.image import ImageGenerationService
from app.services.grok.services.model import ModelService
from app.services.token.manager import get_token_manager

router = APIRouter()

IMAGINE_SESSION_TTL = 600
_IMAGINE_SESSIONS: dict[str, dict] = {}
_IMAGINE_SESSIONS_LOCK = asyncio.Lock()


async def _clean_sessions(now: float) -> None:
    expired = [
        key
        for key, info in _IMAGINE_SESSIONS.items()
        if now - float(info.get("created_at") or 0) > IMAGINE_SESSION_TTL
    ]
    for key in expired:
        _IMAGINE_SESSIONS.pop(key, None)


def _parse_sse_chunk(chunk: str) -> Optional[Dict[str, Any]]:
    if not chunk:
        return None
    event = None
    data_lines: List[str] = []
    for raw in str(chunk).splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return None
    data_str = "\n".join(data_lines)
    if data_str == "[DONE]":
        return None
    try:
        payload = orjson.loads(data_str)
    except orjson.JSONDecodeError:
        return None
    if event and isinstance(payload, dict) and "type" not in payload:
        payload["type"] = event
    return payload


def _normalize_quantity(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        raise ValueError("quantity must be an integer")
    if quantity < 0 or quantity > 200:
        raise ValueError("quantity must be between 0 and 200")
    return quantity


def _normalize_concurrent(value: Any, default: int = 1) -> int:
    if value is None:
        return default
    try:
        concurrent = int(value)
    except (TypeError, ValueError):
        raise ValueError("concurrent must be an integer")
    if concurrent < 1 or concurrent > 6:
        raise ValueError("concurrent must be between 1 and 6")
    return concurrent


def _is_final_image_payload(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    payload_type = str(payload.get("type") or "")
    if payload_type == "image_generation.completed":
        return True
    if payload_type == "image" and (
        payload.get("b64_json") or payload.get("url") or payload.get("image")
    ):
        return True
    stage = str(payload.get("stage") or "").lower()
    if stage == "final" and (
        payload.get("b64_json") or payload.get("url") or payload.get("image")
    ):
        return True
    return False


async def _new_session(
    prompt: str,
    aspect_ratio: str,
    nsfw: Optional[bool],
    quantity: int,
    concurrent: int,
) -> str:
    task_id = uuid.uuid4().hex
    now = time.time()
    async with _IMAGINE_SESSIONS_LOCK:
        await _clean_sessions(now)
        _IMAGINE_SESSIONS[task_id] = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "nsfw": nsfw,
            "quantity": quantity,
            "concurrent": concurrent,
            "created_at": now,
        }
    return task_id


async def _get_session(task_id: str) -> Optional[dict]:
    if not task_id:
        return None
    now = time.time()
    async with _IMAGINE_SESSIONS_LOCK:
        await _clean_sessions(now)
        info = _IMAGINE_SESSIONS.get(task_id)
        if not info:
            return None
        created_at = float(info.get("created_at") or 0)
        if now - created_at > IMAGINE_SESSION_TTL:
            _IMAGINE_SESSIONS.pop(task_id, None)
            return None
        return dict(info)


async def _drop_session(task_id: str) -> None:
    if not task_id:
        return
    async with _IMAGINE_SESSIONS_LOCK:
        _IMAGINE_SESSIONS.pop(task_id, None)


async def _drop_sessions(task_ids: List[str]) -> int:
    if not task_ids:
        return 0
    removed = 0
    async with _IMAGINE_SESSIONS_LOCK:
        for task_id in task_ids:
            if task_id and task_id in _IMAGINE_SESSIONS:
                _IMAGINE_SESSIONS.pop(task_id, None)
                removed += 1
    return removed


@router.websocket("/imagine/ws")
async def public_imagine_ws(websocket: WebSocket):
    session_id = None
    task_id = websocket.query_params.get("task_id")
    if task_id:
        info = await _get_session(task_id)
        if info:
            session_id = task_id

    ok = True
    if session_id is None:
        public_key = get_public_api_key()
        public_enabled = is_public_enabled()
        if not public_key:
            ok = public_enabled
        else:
            key = websocket.query_params.get("public_key")
            ok = key == public_key

    if not ok:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    stop_event = asyncio.Event()
    run_task: Optional[asyncio.Task] = None

    async def _send(payload: dict) -> bool:
        try:
            await websocket.send_text(orjson.dumps(payload).decode())
            return True
        except Exception:
            return False

    async def _stop_run():
        nonlocal run_task
        stop_event.set()
        if run_task and not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except Exception:
                pass
        run_task = None
        stop_event.clear()

    async def _run(
        prompt: str,
        aspect_ratio: str,
        nsfw: Optional[bool],
        quantity: int = 0,
        concurrent: int = 1,
    ):
        model_id = "grok-imagine-1.0"
        model_info = ModelService.get(model_id)
        if not model_info or not model_info.is_image:
            await _send(
                {
                    "type": "error",
                    "message": "Image model is not available.",
                    "code": "model_not_supported",
                }
            )
            return

        token_mgr = await get_token_manager()
        run_id = uuid.uuid4().hex
        final_count = 0
        target_count = max(0, int(quantity or 0))
        batch_size = max(1, int(concurrent or 1))
        stop_reason = "stopped"
        round_index = 0

        await _send(
            {
                "type": "status",
                "status": "running",
                "prompt": prompt,
                "aspect_ratio": aspect_ratio,
                "run_id": run_id,
                "target_count": target_count,
                "batch_size": batch_size,
            }
        )

        while not stop_event.is_set():
            try:
                if target_count > 0 and final_count >= target_count:
                    stop_reason = "quantity_reached"
                    break

                round_index += 1
                remaining = (
                    target_count - final_count if target_count > 0 else batch_size
                )
                request_n = (
                    batch_size if target_count <= 0 else max(1, min(batch_size, remaining))
                )

                await token_mgr.reload_if_stale()
                token = None
                for pool_name in ModelService.pool_candidates_for_model(
                    model_info.model_id
                ):
                    token = token_mgr.get_token(pool_name)
                    if token:
                        break

                if not token:
                    await _send(
                        {
                            "type": "error",
                            "message": "No available tokens. Please try again later.",
                            "code": "rate_limit_exceeded",
                        }
                    )
                    await asyncio.sleep(2)
                    continue

                result = await ImageGenerationService().generate(
                    token_mgr=token_mgr,
                    token=token,
                    model_info=model_info,
                    prompt=prompt,
                    n=request_n,
                    response_format="b64_json",
                    size="1024x1024",
                    aspect_ratio=aspect_ratio,
                    stream=True,
                    enable_nsfw=nsfw,
                )
                if result.stream:
                    async for chunk in result.data:
                        payload = _parse_sse_chunk(chunk)
                        if not payload:
                            continue
                        if isinstance(payload, dict):
                            payload.setdefault("run_id", run_id)
                        await _send(payload)
                        if _is_final_image_payload(payload):
                            final_count += 1
                            if target_count > 0 and final_count >= target_count:
                                stop_reason = "quantity_reached"
                                break
                else:
                    images = [img for img in result.data if img and img != "error"]
                    if images:
                        for img_b64 in images:
                            await _send(
                                {
                                    "type": "image",
                                    "b64_json": img_b64,
                                    "created_at": int(time.time() * 1000),
                                    "aspect_ratio": aspect_ratio,
                                    "run_id": run_id,
                                }
                            )
                            final_count += 1
                            if target_count > 0 and final_count >= target_count:
                                stop_reason = "quantity_reached"
                                break
                    else:
                        await _send(
                            {
                                "type": "error",
                                "message": "Image generation returned empty data.",
                                "code": "empty_image",
                            }
                        )

                await _send(
                    {
                        "type": "status",
                        "status": "round_done",
                        "run_id": run_id,
                        "round": round_index,
                        "generated_count": final_count,
                        "target_count": target_count,
                        "request_n": request_n,
                    }
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Imagine stream error: {e}")
                await _send(
                    {
                        "type": "error",
                        "message": str(e),
                        "code": "internal_error",
                    }
                )
                await asyncio.sleep(1.5)

        await _send(
            {
                "type": "status",
                "status": "stopped",
                "run_id": run_id,
                "reason": stop_reason,
                "generated_count": final_count,
                "target_count": target_count,
            }
        )

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except (RuntimeError, WebSocketDisconnect):
                break

            try:
                payload = orjson.loads(raw)
            except Exception:
                await _send(
                    {
                        "type": "error",
                        "message": "Invalid message format.",
                        "code": "invalid_payload",
                    }
                )
                continue

            action = payload.get("type")
            if action == "start":
                prompt = str(payload.get("prompt") or "").strip()
                if not prompt:
                    await _send(
                        {
                            "type": "error",
                            "message": "Prompt cannot be empty.",
                            "code": "invalid_prompt",
                        }
                    )
                    continue
                aspect_ratio = resolve_aspect_ratio(
                    str(payload.get("aspect_ratio") or "2:3").strip() or "2:3"
                )
                nsfw = payload.get("nsfw")
                if nsfw is not None:
                    nsfw = bool(nsfw)
                try:
                    default_quantity = 0
                    default_concurrent = 1
                    if session_id:
                        session_info = await _get_session(session_id)
                        if session_info:
                            default_quantity = _normalize_quantity(
                                session_info.get("quantity"), default=0
                            )
                            default_concurrent = _normalize_concurrent(
                                session_info.get("concurrent"), default=1
                            )
                    quantity = _normalize_quantity(
                        payload.get("quantity"), default=default_quantity
                    )
                    concurrent = _normalize_concurrent(
                        payload.get("concurrent"), default=default_concurrent
                    )
                except ValueError as e:
                    code = "invalid_quantity"
                    if "concurrent" in str(e):
                        code = "invalid_concurrent"
                    await _send(
                        {
                            "type": "error",
                            "message": str(e),
                            "code": code,
                        }
                    )
                    continue
                await _stop_run()
                run_task = asyncio.create_task(
                    _run(prompt, aspect_ratio, nsfw, quantity, concurrent)
                )
            elif action == "stop":
                await _stop_run()
            else:
                await _send(
                    {
                        "type": "error",
                        "message": "Unknown action.",
                        "code": "invalid_action",
                    }
                )

    except WebSocketDisconnect:
        logger.debug("WebSocket disconnected by client")
    except Exception as e:
        logger.warning(f"WebSocket error: {e}")
    finally:
        await _stop_run()

        try:
            from starlette.websockets import WebSocketState
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(code=1000, reason="Server closing connection")
        except Exception as e:
            logger.debug(f"WebSocket close ignored: {e}")
        if session_id:
            await _drop_session(session_id)


@router.get("/imagine/sse")
async def public_imagine_sse(
    request: Request,
    task_id: str = Query(""),
    prompt: str = Query(""),
    aspect_ratio: str = Query("2:3"),
    quantity: int = Query(0),
    concurrent: int = Query(1),
):
    """Imagine 图片瀑布流（SSE 兜底）"""
    session = None
    if task_id:
        session = await _get_session(task_id)
        if not session:
            raise HTTPException(status_code=404, detail="Task not found")
    else:
        public_key = get_public_api_key()
        public_enabled = is_public_enabled()
        if not public_key:
            if not public_enabled:
                raise HTTPException(status_code=401, detail="Public access is disabled")
        else:
            key = request.query_params.get("public_key")
            if key != public_key:
                raise HTTPException(status_code=401, detail="Invalid authentication token")

    if session:
        prompt = str(session.get("prompt") or "").strip()
        ratio = str(session.get("aspect_ratio") or "2:3").strip() or "2:3"
        nsfw = session.get("nsfw")
        target_count = _normalize_quantity(session.get("quantity"), default=0)
        batch_size = _normalize_concurrent(session.get("concurrent"), default=1)
    else:
        prompt = (prompt or "").strip()
        if not prompt:
            raise HTTPException(status_code=400, detail="Prompt cannot be empty")
        ratio = str(aspect_ratio or "2:3").strip() or "2:3"
        ratio = resolve_aspect_ratio(ratio)
        try:
            target_count = _normalize_quantity(quantity, default=0)
            batch_size = _normalize_concurrent(concurrent, default=1)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        nsfw = request.query_params.get("nsfw")
        if nsfw is not None:
            nsfw = str(nsfw).lower() in ("1", "true", "yes", "on")

    async def event_stream():
        try:
            model_id = "grok-imagine-1.0"
            model_info = ModelService.get(model_id)
            if not model_info or not model_info.is_image:
                yield (
                    f"data: {orjson.dumps({'type': 'error', 'message': 'Image model is not available.', 'code': 'model_not_supported'}).decode()}\n\n"
                )
                return

            token_mgr = await get_token_manager()
            sequence = 0
            run_id = uuid.uuid4().hex
            final_count = 0
            stop_reason = "stopped"
            round_index = 0

            yield (
                f"data: {orjson.dumps({'type': 'status', 'status': 'running', 'prompt': prompt, 'aspect_ratio': ratio, 'run_id': run_id, 'target_count': target_count, 'batch_size': batch_size}).decode()}\n\n"
            )

            while True:
                if await request.is_disconnected():
                    break
                if task_id:
                    session_alive = await _get_session(task_id)
                    if not session_alive:
                        break

                try:
                    if target_count > 0 and final_count >= target_count:
                        stop_reason = "quantity_reached"
                        break

                    round_index += 1
                    remaining = (
                        target_count - final_count if target_count > 0 else batch_size
                    )
                    request_n = (
                        batch_size if target_count <= 0 else max(1, min(batch_size, remaining))
                    )

                    await token_mgr.reload_if_stale()
                    token = None
                    for pool_name in ModelService.pool_candidates_for_model(
                        model_info.model_id
                    ):
                        token = token_mgr.get_token(pool_name)
                        if token:
                            break

                    if not token:
                        yield (
                            f"data: {orjson.dumps({'type': 'error', 'message': 'No available tokens. Please try again later.', 'code': 'rate_limit_exceeded'}).decode()}\n\n"
                        )
                        await asyncio.sleep(2)
                        continue

                    result = await ImageGenerationService().generate(
                        token_mgr=token_mgr,
                        token=token,
                        model_info=model_info,
                        prompt=prompt,
                        n=request_n,
                        response_format="b64_json",
                        size="1024x1024",
                        aspect_ratio=ratio,
                        stream=True,
                        enable_nsfw=nsfw,
                    )
                    if result.stream:
                        async for chunk in result.data:
                            payload = _parse_sse_chunk(chunk)
                            if not payload:
                                continue
                            if isinstance(payload, dict):
                                payload.setdefault("run_id", run_id)
                            yield f"data: {orjson.dumps(payload).decode()}\n\n"
                            if _is_final_image_payload(payload):
                                final_count += 1
                                if target_count > 0 and final_count >= target_count:
                                    stop_reason = "quantity_reached"
                                    break
                    else:
                        images = [img for img in result.data if img and img != "error"]
                        if images:
                            for img_b64 in images:
                                sequence += 1
                                payload = {
                                    "type": "image",
                                    "b64_json": img_b64,
                                    "sequence": sequence,
                                    "created_at": int(time.time() * 1000),
                                    "aspect_ratio": ratio,
                                    "run_id": run_id,
                                }
                                yield f"data: {orjson.dumps(payload).decode()}\n\n"
                                final_count += 1
                                if target_count > 0 and final_count >= target_count:
                                    stop_reason = "quantity_reached"
                                    break
                        else:
                            yield (
                                f"data: {orjson.dumps({'type': 'error', 'message': 'Image generation returned empty data.', 'code': 'empty_image'}).decode()}\n\n"
                            )

                    yield (
                        f"data: {orjson.dumps({'type': 'status', 'status': 'round_done', 'run_id': run_id, 'round': round_index, 'generated_count': final_count, 'target_count': target_count, 'request_n': request_n}).decode()}\n\n"
                    )
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.warning(f"Imagine SSE error: {e}")
                    yield (
                        f"data: {orjson.dumps({'type': 'error', 'message': str(e), 'code': 'internal_error'}).decode()}\n\n"
                    )
                    await asyncio.sleep(1.5)

                if stop_reason == "quantity_reached":
                    break

            yield (
                f"data: {orjson.dumps({'type': 'status', 'status': 'stopped', 'run_id': run_id, 'reason': stop_reason, 'generated_count': final_count, 'target_count': target_count}).decode()}\n\n"
            )
        finally:
            if task_id:
                await _drop_session(task_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.get("/imagine/config")
async def public_imagine_config():
    return {
        "final_min_bytes": int(get_config("image.final_min_bytes") or 0),
        "medium_min_bytes": int(get_config("image.medium_min_bytes") or 0),
        "nsfw": bool(get_config("image.nsfw")),
    }


class ImagineStartRequest(BaseModel):
    prompt: str
    aspect_ratio: Optional[str] = "2:3"
    nsfw: Optional[bool] = None
    quantity: Optional[int] = 0
    concurrent: Optional[int] = 1


@router.post("/imagine/start", dependencies=[Depends(verify_public_key)])
async def public_imagine_start(data: ImagineStartRequest):
    prompt = (data.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")
    ratio = resolve_aspect_ratio(str(data.aspect_ratio or "2:3").strip() or "2:3")
    try:
        quantity = _normalize_quantity(data.quantity, default=0)
        concurrent = _normalize_concurrent(data.concurrent, default=1)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    task_id = await _new_session(prompt, ratio, data.nsfw, quantity, concurrent)
    return {
        "task_id": task_id,
        "aspect_ratio": ratio,
        "quantity": quantity,
        "concurrent": concurrent,
    }


class ImagineStopRequest(BaseModel):
    task_ids: List[str]


@router.post("/imagine/stop", dependencies=[Depends(verify_public_key)])
async def public_imagine_stop(data: ImagineStopRequest):
    removed = await _drop_sessions(data.task_ids or [])
    return {"status": "success", "removed": removed}

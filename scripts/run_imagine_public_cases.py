import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "http://127.0.0.1:8000"
START_URL = f"{BASE_URL}/v1/public/imagine/start"
SSE_URL = f"{BASE_URL}/v1/public/imagine/sse"
STOP_URL = f"{BASE_URL}/v1/public/imagine/stop"
MAX_WAIT_SECONDS = 180


@dataclass
class CaseResult:
    case: str
    status: str = "pending"
    task_id: Optional[str] = None
    request_ns: List[int] = field(default_factory=list)
    reason: Optional[str] = None
    generated_count: Optional[int] = None
    errors: List[str] = field(default_factory=list)


class SharedState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running_seen = False
        self.round_done_seen = 0
        self.stopped_seen = False


def create_task(session: requests.Session, payload: Dict[str, Any]) -> str:
    resp = session.post(START_URL, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    task_id = str(data.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError(f"start missing task_id: {data}")
    return task_id


def stop_task(session: requests.Session, task_id: str, errors: List[str]) -> None:
    try:
        resp = session.post(STOP_URL, json={"task_ids": [task_id]}, timeout=20)
        if resp.status_code >= 400:
            errors.append(f"stop failed: HTTP {resp.status_code} {resp.text[:300]}")
    except Exception as exc:
        errors.append(f"stop exception: {type(exc).__name__}: {exc}")


def parse_sse_event(lines: List[str]) -> Optional[Dict[str, Any]]:
    if not lines:
        return None
    data_lines: List[str] = []
    for line in lines:
        if line.startswith("data:"):
            data_lines.append(line[5:].strip())
    if not data_lines:
        return None
    raw = "\n".join(data_lines)
    if raw == "[DONE]":
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "error", "message": f"invalid_json:{raw[:200]}"}


def run_case(
    session: requests.Session,
    case_name: str,
    prompt: str,
    concurrent: int,
    quantity: int,
    manual_stop_mode: Optional[str] = None,
) -> CaseResult:
    result = CaseResult(case=case_name)
    shared = SharedState()

    try:
        task_id = create_task(
            session,
            {
                "prompt": prompt,
                "aspect_ratio": "2:3",
                "nsfw": False,
                "quantity": quantity,
                "concurrent": concurrent,
            },
        )
        result.task_id = task_id
    except Exception as exc:
        result.status = "failed"
        result.errors.append(f"start exception: {type(exc).__name__}: {exc}")
        return result

    done_event = threading.Event()

    def reader() -> None:
        try:
            with session.get(SSE_URL, params={"task_id": result.task_id}, stream=True, timeout=(10, 120)) as resp:
                if resp.status_code >= 400:
                    result.errors.append(f"sse http {resp.status_code}: {resp.text[:300]}")
                    result.status = "failed"
                    done_event.set()
                    return

                event_lines: List[str] = []
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if raw_line is None:
                        continue
                    line = str(raw_line)
                    if line == "":
                        payload = parse_sse_event(event_lines)
                        event_lines = []
                        if not payload:
                            continue

                        if payload.get("type") == "error":
                            result.errors.append(str(payload.get("message") or "unknown_error"))

                        if payload.get("status") == "running":
                            with shared.lock:
                                shared.running_seen = True

                        if payload.get("status") == "round_done":
                            n = payload.get("request_n")
                            if isinstance(n, int):
                                result.request_ns.append(n)
                            gc = payload.get("generated_count")
                            if isinstance(gc, int):
                                result.generated_count = gc
                            with shared.lock:
                                shared.round_done_seen += 1

                        if payload.get("status") == "stopped":
                            reason = payload.get("reason")
                            if reason is not None:
                                result.reason = str(reason)
                            gc = payload.get("generated_count")
                            if isinstance(gc, int):
                                result.generated_count = gc
                            with shared.lock:
                                shared.stopped_seen = True
                            result.status = "ok"
                            done_event.set()
                            return
                    else:
                        event_lines.append(line)
        except Exception as exc:
            result.errors.append(f"sse exception: {type(exc).__name__}: {exc}")
            if result.status == "pending":
                result.status = "failed"
            done_event.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    start_at = time.time()
    stop_sent = False
    manual_deadline: Optional[float] = None

    while True:
        if done_event.wait(timeout=0.2):
            break

        elapsed = time.time() - start_at
        if elapsed > MAX_WAIT_SECONDS:
            result.status = "timeout"
            result.errors.append(f"timeout after {MAX_WAIT_SECONDS}s")
            if result.task_id:
                stop_task(session, result.task_id, result.errors)
            break

        if manual_stop_mode:
            with shared.lock:
                running_seen = shared.running_seen
                round_done_seen = shared.round_done_seen

            if manual_stop_mode == "after_first_round" and round_done_seen >= 1 and not stop_sent:
                if result.task_id:
                    stop_task(session, result.task_id, result.errors)
                stop_sent = True

            if manual_stop_mode == "after_running_delay":
                if running_seen and manual_deadline is None:
                    manual_deadline = time.time() + 4.0
                if manual_deadline is not None and time.time() >= manual_deadline and not stop_sent:
                    if result.task_id:
                        stop_task(session, result.task_id, result.errors)
                    stop_sent = True

    t.join(timeout=2)
    if result.status == "pending":
        result.status = "failed" if result.errors else "unknown"
    return result


def main() -> None:
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    cases = [
        {
            "case_name": "case_a_3_1",
            "prompt": "A minimal monochrome product photo of a ceramic cup",
            "concurrent": 3,
            "quantity": 1,
            "manual_stop_mode": None,
        },
        {
            "case_name": "case_b_3_8",
            "prompt": "A futuristic city street at sunset, ultra detailed",
            "concurrent": 3,
            "quantity": 8,
            "manual_stop_mode": None,
        },
        {
            "case_name": "case_c_unlimited",
            "prompt": "An abstract geometric poster with bold colors",
            "concurrent": 3,
            "quantity": 0,
            "manual_stop_mode": "after_first_round",
        },
        {
            "case_name": "case_d_manual_stop",
            "prompt": "A cinematic mountain landscape with dramatic clouds",
            "concurrent": 3,
            "quantity": 20,
            "manual_stop_mode": "after_running_delay",
        },
    ]

    summary: Dict[str, Any] = {"base_url": BASE_URL, "results": []}

    for item in cases:
        result = run_case(
            session=session,
            case_name=item["case_name"],
            prompt=item["prompt"],
            concurrent=item["concurrent"],
            quantity=item["quantity"],
            manual_stop_mode=item["manual_stop_mode"],
        )
        summary["results"].append(
            {
                "case": result.case,
                "status": result.status,
                "task_id": result.task_id,
                "request_ns": result.request_ns,
                "reason": result.reason,
                "generated_count": result.generated_count,
                "errors": result.errors,
            }
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

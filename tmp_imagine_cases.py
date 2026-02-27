import json
import time
from typing import Any, Dict, List, Optional

try:
    import requests  # type: ignore
except Exception:
    # Minimal requests-compatible shim using urllib for offline environments.
    import urllib.parse
    import urllib.request
    import urllib.error

    class _Resp:
        def __init__(self, raw, status_code: int, url: str):
            self._raw = raw
            self.status_code = status_code
            self.url = url
            self._text_cache = None

        @property
        def text(self) -> str:
            if self._text_cache is None:
                data = self._raw.read()
                self._text_cache = data.decode("utf-8", errors="replace")
            return self._text_cache

        def json(self):
            return json.loads(self.text)

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}: {self.url} {self.text[:500]}")

        def iter_lines(self, decode_unicode=True):
            while True:
                line = self._raw.readline()
                if not line:
                    break
                if decode_unicode:
                    yield line.decode("utf-8", errors="replace").rstrip("\r\n")
                else:
                    yield line.rstrip(b"\r\n")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            try:
                self._raw.close()
            except Exception:
                pass

    class _RequestsShim:
        @staticmethod
        def post(url: str, json: Optional[dict] = None, timeout: int = 30):
            data = None
            headers = {}
            if json is not None:
                data = str.encode(__import__("json").dumps(json))
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=data, method="POST", headers=headers)
            try:
                raw = urllib.request.urlopen(req, timeout=timeout)
                return _Resp(raw, getattr(raw, "status", 200), url)
            except urllib.error.HTTPError as e:
                return _Resp(e, e.code, url)

        @staticmethod
        def get(url: str, params: Optional[dict] = None, stream: bool = False, timeout=30):
            if params:
                qs = urllib.parse.urlencode(params)
                joiner = "&" if "?" in url else "?"
                url = f"{url}{joiner}{qs}"
            real_timeout = timeout[1] if isinstance(timeout, tuple) else timeout
            req = urllib.request.Request(url, method="GET")
            try:
                raw = urllib.request.urlopen(req, timeout=real_timeout)
                return _Resp(raw, getattr(raw, "status", 200), url)
            except urllib.error.HTTPError as e:
                return _Resp(e, e.code, url)

    requests = _RequestsShim()  # type: ignore

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT_SEC = 120


def safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return {"raw": getattr(resp, "text", "")}


def start_task(prompt: str, quantity: int, concurrent: int) -> str:
    url = f"{BASE_URL}/v1/public/imagine/start"
    payload = {
        "prompt": prompt,
        "aspect_ratio": "2:3",
        "nsfw": True,
        "quantity": quantity,
        "concurrent": concurrent,
    }
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = safe_json(r)
    task_id = data.get("task_id") or data.get("id")
    if not task_id:
        raise RuntimeError(f"start missing task_id: {json.dumps(data, ensure_ascii=False)}")
    return str(task_id)


def stop_task(task_id: str) -> Dict[str, Any]:
    url = f"{BASE_URL}/v1/public/imagine/stop"
    payload = {"task_id": task_id}
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return safe_json(r)


def parse_sse_data_line(line: str) -> Dict[str, Any]:
    if not line.startswith("data:"):
        raise ValueError(f"non-data-line: {line}")
    raw = line[5:].strip()
    if not raw:
        raise ValueError("empty-data")
    return json.loads(raw)


def run_case(case_name: str, quantity: int, concurrent: int, manual_stop_mode: Optional[str] = None) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "case": case_name,
        "status": "failed",
        "request_ns": [],
        "reason": None,
        "generated_count": None,
        "image_events": 0,
        "errors": [],
        "elapsed_sec": 0.0,
    }

    start_ts = time.monotonic()
    deadline = start_ts + TIMEOUT_SEC

    try:
        task_id = start_task(
            prompt=f"Integration test {case_name} {int(time.time())}",
            quantity=quantity,
            concurrent=concurrent,
        )
    except Exception as e:
        result["errors"].append(f"start_exception: {e}")
        result["elapsed_sec"] = round(time.monotonic() - start_ts, 3)
        return result

    stopped = False
    stop_sent = False

    sse_url = f"{BASE_URL}/v1/public/imagine/sse"
    params = {"task_id": task_id}

    try:
        with requests.get(sse_url, params=params, stream=True, timeout=(10, 125)) as resp:
            resp.raise_for_status()
            for raw_line in resp.iter_lines(decode_unicode=True):
                now = time.monotonic()
                if now > deadline:
                    result["errors"].append("timeout: exceeded 120s")
                    break

                if raw_line is None:
                    continue
                line = raw_line.strip()
                if not line:
                    continue
                if not line.startswith("data:"):
                    continue

                try:
                    evt = parse_sse_data_line(line)
                except Exception as pe:
                    result["errors"].append(f"sse_parse_exception: {pe}; line={line}")
                    continue

                status = evt.get("status")

                if status == "round_done":
                    rn = evt.get("request_n")
                    if rn is not None:
                        result["request_ns"].append(rn)
                    if manual_stop_mode == "first_event" and not stop_sent:
                        try:
                            stop_task(task_id)
                            stop_sent = True
                        except Exception as se:
                            result["errors"].append(f"stop_exception: {se}")

                if status == "image":
                    result["image_events"] += 1
                    if manual_stop_mode == "first_event" and not stop_sent:
                        try:
                            stop_task(task_id)
                            stop_sent = True
                        except Exception as se:
                            result["errors"].append(f"stop_exception: {se}")

                if manual_stop_mode == "after_5s" and not stop_sent and (now - start_ts) >= 5.0:
                    try:
                        stop_task(task_id)
                        stop_sent = True
                    except Exception as se:
                        result["errors"].append(f"stop_exception: {se}")

                if status == "stopped":
                    result["reason"] = evt.get("reason")
                    result["generated_count"] = evt.get("generated_count")
                    stopped = True
                    break

            if not stopped and manual_stop_mode == "after_5s" and not stop_sent and time.monotonic() <= deadline:
                try:
                    stop_task(task_id)
                    stop_sent = True
                except Exception as se:
                    result["errors"].append(f"late_stop_exception: {se}")

    except Exception as e:
        result["errors"].append(f"sse_exception: {e}")

    result["elapsed_sec"] = round(time.monotonic() - start_ts, 3)
    result["status"] = "ok" if stopped and not result["errors"] else ("stopped_with_errors" if stopped else "failed")
    return result


def main() -> None:
    cases = [
        ("case1(3/1)", 3, 1, None),
        ("case2(3/8)", 3, 8, None),
        ("case3(infinite_stop_on_first)", 0, 3, "first_event"),
        ("case4(manual_stop_5s)", 20, 3, "after_5s"),
    ]

    outputs: List[Dict[str, Any]] = []
    for name, q, c, mode in cases:
        outputs.append(run_case(name, q, c, mode))

    print(json.dumps(outputs, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

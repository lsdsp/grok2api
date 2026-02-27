import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

BASE = "http://127.0.0.1:8000"
PY = os.path.join('.venv', 'Scripts', 'python.exe')


def http_get(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, method='GET')
    return urllib.request.urlopen(req, timeout=timeout)


def http_post_json(url: str, payload: Dict[str, Any], timeout: float = 15.0):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    req.add_header('Content-Type', 'application/json')
    return urllib.request.urlopen(req, timeout=timeout)


def wait_server(timeout=25.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with http_get(BASE + '/docs', timeout=1.5) as r:
                if getattr(r, 'status', 200) < 500:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def parse_sse_line(line: str) -> Optional[Dict[str, Any]]:
    if not line:
        return None
    line = line.strip()
    if not line.startswith('data:'):
        return None
    payload = line[5:].strip()
    if not payload or payload == '[DONE]':
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def start_task(prompt: str, quantity: int, concurrent: int, errors: List[str]) -> Optional[str]:
    body = {
        'prompt': prompt,
        'aspect_ratio': '1:1',
        'quantity': quantity,
        'concurrent': concurrent,
    }
    try:
        with http_post_json(BASE + '/v1/public/imagine/start', body, timeout=15) as r:
            raw = r.read().decode('utf-8', errors='ignore')
            if getattr(r, 'status', 200) != 200:
                errors.append(f'start_http_{getattr(r, "status", "unknown")}:{raw[:300]}')
                return None
            j = json.loads(raw)
            tid = j.get('task_id')
            if not tid:
                errors.append('start_no_task_id')
                return None
            return tid
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore') if hasattr(e, 'read') else ''
        errors.append(f'start_http_{e.code}:{body[:300]}')
    except Exception as e:
        errors.append(f'start_exc:{type(e).__name__}:{e}')
    return None


def stop_task(task_id: str, errors: List[str]) -> None:
    try:
        with http_post_json(BASE + '/v1/public/imagine/stop', {'task_ids': [task_id]}, timeout=15) as r:
            _ = r.read()
            if getattr(r, 'status', 200) != 200:
                errors.append(f'stop_http_{getattr(r, "status", "unknown")}')
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='ignore') if hasattr(e, 'read') else ''
        errors.append(f'stop_http_{e.code}:{body[:300]}')
    except Exception as e:
        errors.append(f'stop_exc:{type(e).__name__}:{e}')


def run_case(name: str, quantity: int, concurrent: int, stop_mode: str) -> Dict[str, Any]:
    t0 = time.time()
    errors: List[str] = []
    request_ns: List[int] = []
    reason: Optional[str] = None
    generated_count: Optional[int] = None
    image_events = 0

    task_id = start_task(prompt=f'codex test {name}', quantity=quantity, concurrent=concurrent, errors=errors)
    if not task_id:
        return {
            'case': name,
            'status': 'failed',
            'request_ns': request_ns,
            'reason': reason,
            'generated_count': generated_count,
            'image_events': image_events,
            'elapsed_sec': round(time.time()-t0, 3),
            'errors': errors,
        }

    stop_sent = False
    first_signal_received = False

    def delayed_stop():
        nonlocal stop_sent
        time.sleep(5)
        stop_task(task_id, errors)
        stop_sent = True

    if stop_mode == 'manual_after_5s':
        threading.Thread(target=delayed_stop, daemon=True).start()

    sse_url = BASE + '/v1/public/imagine/sse?' + urllib.parse.urlencode({'task_id': task_id})

    try:
        with http_get(sse_url, timeout=120) as resp:
            if getattr(resp, 'status', 200) != 200:
                raw = resp.read().decode('utf-8', errors='ignore')
                errors.append(f'sse_http_{getattr(resp, "status", "unknown")}:{raw[:300]}')
            else:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    try:
                        text = line.decode('utf-8', errors='ignore')
                    except Exception:
                        continue
                    data = parse_sse_line(text)
                    if not data:
                        continue

                    t = data.get('type')
                    if t in ('image', 'image_generation.completed'):
                        image_events += 1
                        if stop_mode == 'infinite_stop_on_first' and not stop_sent:
                            stop_task(task_id, errors)
                            stop_sent = True
                            first_signal_received = True

                    if data.get('status') == 'round_done':
                        rn = data.get('request_n')
                        if isinstance(rn, int):
                            request_ns.append(rn)
                        if stop_mode == 'infinite_stop_on_first' and not stop_sent:
                            stop_task(task_id, errors)
                            stop_sent = True
                            first_signal_received = True

                    if data.get('status') == 'stopped':
                        reason = data.get('reason')
                        gc = data.get('generated_count')
                        generated_count = gc if isinstance(gc, int) else None
                        break
    except TimeoutError:
        errors.append('case_timeout_120s')
    except Exception as e:
        errors.append(f'sse_exc:{type(e).__name__}:{e}')

    if stop_mode == 'infinite_stop_on_first' and not first_signal_received and not stop_sent:
        stop_task(task_id, errors)
        stop_sent = True

    elapsed = round(time.time() - t0, 3)
    status = 'ok' if reason is not None and not any('timeout' in x for x in errors) else 'failed'
    return {
        'case': name,
        'status': status,
        'request_ns': request_ns,
        'reason': reason,
        'generated_count': generated_count,
        'image_events': image_events,
        'elapsed_sec': elapsed,
        'errors': errors,
    }


def main():
    results: List[Dict[str, Any]] = []
    proc = None
    try:
        if not os.path.exists(PY):
            print(json.dumps([{
                'case': 'bootstrap',
                'status': 'failed',
                'request_ns': [],
                'reason': None,
                'generated_count': None,
                'image_events': 0,
                'elapsed_sec': 0,
                'errors': [f'missing_python:{PY}'],
            }], ensure_ascii=False))
            return

        proc = subprocess.Popen(
            [PY, 'main.py'],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=os.getcwd(),
        )

        if not wait_server(25):
            results.append({
                'case': 'bootstrap',
                'status': 'failed',
                'request_ns': [],
                'reason': None,
                'generated_count': None,
                'image_events': 0,
                'elapsed_sec': 25,
                'errors': ['server_not_ready_within_25s'],
            })
            print(json.dumps(results, ensure_ascii=False))
            return

        results.append(run_case('3/1', quantity=1, concurrent=3, stop_mode='normal'))
        results.append(run_case('3/8', quantity=8, concurrent=3, stop_mode='normal'))
        results.append(run_case('无限', quantity=0, concurrent=3, stop_mode='infinite_stop_on_first'))
        results.append(run_case('手动停止', quantity=20, concurrent=3, stop_mode='manual_after_5s'))

        print(json.dumps(results, ensure_ascii=False))
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


if __name__ == '__main__':
    main()

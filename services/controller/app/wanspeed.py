"""Общий спидтест канала: напрямую через WAN и через текущий VPN.

Замер серверов (speedtest.py) отвечает на вопрос «какой сервер быстрее». Здесь
вопрос другой — «сколько сейчас даёт сам интернет и сколько от него остаётся
в туннеле». Тест идёт с самого роутера, поэтому показывает потолок канала без
Wi-Fi и сетевух клиентов.

Качает curl, а не Python: на слабых ARM-ядрах интерпретатор упирается в CPU
задолго до гигабита и честного числа не покажет. Несколько параллельных
потоков — потому что один TCP-поток через дальний туннель редко выбирает
полосу целиком, а провайдерские спидтесты тоже меряют в несколько потоков.
"""

import json
import logging
import os
import statistics
import subprocess
import threading
import time

from . import apply, settings, xray_build

log = logging.getLogger("circle.wanspeed")

TRACE_URL = "https://speed.cloudflare.com/cdn-cgi/trace"
# Больше ~50 МБ за запрос Cloudflare не отдаёт, поэтому каждый поток качает в
# цикле, пока не выйдет время.
DOWN_URL = "https://speed.cloudflare.com/__down?bytes=50000000"
UP_URL = "https://speed.cloudflare.com/__up"
UP_BLOB = settings.CACHE_DIR / "wanspeed-up.bin"
UP_BLOB_SIZE = 16 * 1024 * 1024
RESULT_PATH = settings.DATA / "config" / "wanspeed.json"
STREAMS = 4

_lock = threading.Lock()
_job = {"running": False, "stage": "", "progress": 0.0, "started": 0, "error": None}


def _load_result():
    try:
        return json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def status():
    with _lock:
        job = dict(_job)
    return {"job": job, "result": _load_result(),
            "vpn_available": _vpn_available()}


def _vpn_available():
    return xray_build.effective()["vpn"] and apply.xray_listening(xray_build.HEALTH_PORT)


def _curl(args, timeout):
    """curl с -w JSON. На таймауте (код 28) curl всё равно печатает счётчики —
    именно так и заканчивается каждый замер по времени."""
    cmd = ["curl", "-s", "-o", os.devnull, "-w", "%{json}", "--connect-timeout", "10", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        return {}
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _proxy_args(path):
    return ["-x", f"http://127.0.0.1:{xray_build.HEALTH_PORT}"] if path == "vpn" else []


def _trace(path):
    """Отклик и выход: несколько маленьких запросов, берём лучший."""
    times, info = [], {}
    for _ in range(5):
        cmd = ["curl", "-s", "--connect-timeout", "8", "-m", "10", *_proxy_args(path),
               "-w", "\n@@%{time_starttransfer}", TRACE_URL]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            continue
        body, _, timing = proc.stdout.rpartition("\n@@")
        if proc.returncode != 0 or not body:
            continue
        times.append(float(timing) * 1000)
        for line in body.splitlines():
            key, _, value = line.partition("=")
            if key in ("ip", "loc", "colo"):
                info[key] = value
    if not times:
        return None
    return {"ping_ms": round(min(times), 1),
            "jitter_ms": round(statistics.pstdev(times), 1) if len(times) > 1 else 0.0,
            **info}


def _stream(path, kind, deadline, totals, index):
    while True:
        remaining = deadline - time.monotonic()
        if remaining < 0.5:
            return
        args = [*_proxy_args(path), "-m", f"{remaining:.1f}"]
        if kind == "down":
            data = _curl([*args, DOWN_URL], remaining)
            got = int(data.get("size_download") or 0)
        else:
            data = _curl([*args, "-X", "POST", "--data-binary", f"@{UP_BLOB}",
                          "-H", "Content-Type: application/octet-stream", UP_URL], remaining)
            got = int(data.get("size_upload") or 0)
        totals[index] += got
        if not got:
            return


def _throughput(path, kind, duration):
    totals = [0] * STREAMS
    started = time.monotonic()
    deadline = started + duration
    threads = [threading.Thread(target=_stream, args=(path, kind, deadline, totals, i), daemon=True)
               for i in range(STREAMS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(duration + 30)
    elapsed = max(min(time.monotonic(), deadline) - started, 0.5)
    total = sum(totals)
    return round(total * 8 / elapsed / 1_000_000, 1), total


def _ensure_blob():
    if not UP_BLOB.exists() or UP_BLOB.stat().st_size != UP_BLOB_SIZE:
        UP_BLOB.write_bytes(os.urandom(UP_BLOB_SIZE))


def _stage(text, progress):
    with _lock:
        _job["stage"] = text
        _job["progress"] = progress


def _run(paths, duration):
    result = {"ts": int(time.time()), "duration": duration, "streams": STREAMS, "paths": {}}
    try:
        _ensure_blob()
        steps = len(paths) * 3
        step = 0
        for path in paths:
            label = "напрямую" if path == "direct" else "через VPN"
            entry = {}
            _stage(f"Отклик {label}", step / steps)
            trace = _trace(path)
            step += 1
            if not trace:
                entry["error"] = "сервер замера не ответил"
                result["paths"][path] = entry
                step += 2
                continue
            entry.update(trace)
            _stage(f"Скачивание {label}", step / steps)
            entry["down_mbps"], entry["down_bytes"] = _throughput(path, "down", duration)
            step += 1
            _stage(f"Отдача {label}", step / steps)
            entry["up_mbps"], entry["up_bytes"] = _throughput(path, "up", duration)
            step += 1
            result["paths"][path] = entry
        RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        with _lock:
            _job["error"] = None
    except Exception as exc:                           # noqa: BLE001
        log.exception("спидтест не удался")
        with _lock:
            _job["error"] = str(exc)
    finally:
        with _lock:
            _job.update({"running": False, "stage": "", "progress": 1.0})


def start(paths=None, duration=10):
    paths = [p for p in (paths or ["direct", "vpn"]) if p in ("direct", "vpn")]
    if "vpn" in paths and not _vpn_available():
        paths.remove("vpn")
    if not paths:
        raise ValueError("нечего мерить: VPN не поднят")
    duration = max(5, min(int(duration), 30))
    with _lock:
        if _job["running"]:
            raise RuntimeError("спидтест уже идёт")
        _job.update({"running": True, "stage": "Готовлюсь", "progress": 0.0,
                     "started": int(time.time()), "error": None, "paths": paths})
    threading.Thread(target=_run, args=(paths, duration), name="wanspeed", daemon=True).start()
    return status()

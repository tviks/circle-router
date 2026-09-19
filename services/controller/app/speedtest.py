"""Замер реальной скорости через конкретный VLESS-сервер.

Пинг до хоста говорит только о задержке до входа. Чтобы понять, сколько сервер
реально отдаёт, поднимаем временный экземпляр xray с одним этим аутбаундом и
локальным HTTP-инбаундом, качаем через него файл и считаем скорость. Основной
xray при этом не трогается — клиенты в LAN ничего не замечают.
"""

import json
import logging
import socket
import subprocess
import threading
import time

import httpx

from . import settings, state, xray_build

log = logging.getLogger("circle.speedtest")

TRACE_URL = "https://speed.cloudflare.com/cdn-cgi/trace"
STARTUP_TIMEOUT = 10.0
# Страховка от бесконечного цикла, если сервер отдаёт по чуть-чуть.
MAX_DOWNLOAD_ROUNDS = 60

_lock = threading.Lock()
_job = {
    "running": False,
    "started_at": 0,
    "current": None,
    "queue": [],
    "done": [],
    "results": {},
}


def status():
    with _lock:
        return json.loads(json.dumps(_job))


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_port(port, timeout=STARTUP_TIMEOUT):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            try:
                sock.connect(("127.0.0.1", port))
                return True
            except OSError:
                time.sleep(0.2)
    return False


def _probe_config(outbound, port):
    return {
        "log": {"loglevel": "error"},
        "inbounds": [{
            "tag": "probe-in",
            "listen": "127.0.0.1",
            "port": port,
            "protocol": "http",
            "settings": {"allowTransparent": False},
        }],
        # Первый аутбаунд — дефолтный, поэтому весь трафик пробы уходит в него.
        "outbounds": [outbound, {"tag": "direct", "protocol": "freedom"}],
    }


def _measure(proxy_url, test_url, duration):
    result = {"latency_ms": None, "exit_ip": None, "location": None,
              "bytes": 0, "seconds": 0.0, "mbps": 0.0}

    timeout = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)
    with httpx.Client(proxy=proxy_url, timeout=timeout, follow_redirects=True) as client:
        started = time.perf_counter()
        try:
            trace = client.get(TRACE_URL)
            result["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
            for line in trace.text.splitlines():
                if line.startswith("ip="):
                    result["exit_ip"] = line[3:]
                elif line.startswith("loc="):
                    result["location"] = line[4:]
        except httpx.HTTPError as exc:
            raise RuntimeError(f"проба не прошла: {exc}") from exc

        # Cloudflare отдаёт максимум ~50 МБ за запрос, а быстрый канал съедает
        # их меньше чем за секунду. Поэтому качаем в цикле, пока не выйдет
        # отведённое время: окно замера задаёт длительность, а не размер файла.
        downloaded = 0
        started = None
        deadline = None
        for _ in range(MAX_DOWNLOAD_ROUNDS):
            with client.stream("GET", test_url) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(65536):
                    if started is None:
                        # Отсчёт с первого байта: TLS-handshake и время запроса
                        # к скорости канала отношения не имеют.
                        started = time.perf_counter()
                        deadline = started + duration
                    downloaded += len(chunk)
                    if time.perf_counter() >= deadline:
                        break
            if started is None:
                raise RuntimeError("сервер не отдал ни байта")
            if time.perf_counter() >= deadline:
                break
        elapsed = max(time.perf_counter() - started, 0.001)

    result["bytes"] = downloaded
    result["seconds"] = round(elapsed, 2)
    result["mbps"] = round(downloaded * 8 / elapsed / 1_000_000, 2)
    return result


def test_server(server, cfg):
    """Один замер. Бросает исключение с человекочитаемым текстом при ошибке."""
    outbound = xray_build.outbound_for(server)
    if not outbound:
        raise ValueError("у сервера нет ни ссылки, ни outbound")

    speed_cfg = cfg.get("speedtest") or {}
    test_url = speed_cfg.get("url") or "https://speed.cloudflare.com/__down?bytes=50000000"
    duration = max(3, min(int(speed_cfg.get("duration") or 8), 30))

    port = _free_port()
    config_path = settings.XRAY_CONFIG_PATH.parent / f"probe-{port}.json"
    config_path.write_text(
        json.dumps(_probe_config(outbound, port), ensure_ascii=False), encoding="utf-8")

    ok, output = xray_build.validate(config_path)
    if not ok:
        config_path.unlink(missing_ok=True)
        raise ValueError(xray_build._first_error_line(output))

    process = subprocess.Popen(
        [settings.XRAY_BIN, "run", "-config", str(config_path), "-format=json"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        if not _wait_port(port):
            raise RuntimeError("xray не поднял пробный инбаунд")
        return _measure(f"http://127.0.0.1:{port}", test_url, duration)
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        config_path.unlink(missing_ok=True)


def _persist(server_id, result):
    def mutate(cfg):
        for server in cfg.get("servers") or []:
            if server.get("id") == server_id:
                server["last_test"] = result
    state.update(mutate)


def _run(server_ids):
    cfg = state.load()
    by_id = {s["id"]: s for s in cfg.get("servers") or []}

    for server_id in server_ids:
        server = by_id.get(server_id)
        with _lock:
            _job["current"] = server_id
            _job["queue"] = [i for i in _job["queue"] if i != server_id]
        if not server:
            continue

        started = time.time()
        try:
            measurement = test_server(server, cfg)
            result = {"ok": True, "ts": int(started), **measurement}
        except Exception as exc:                       # noqa: BLE001
            result = {"ok": False, "ts": int(started), "error": str(exc)[:300]}
            log.info("замер %s не удался: %s", server.get("name"), exc)

        _persist(server_id, result)
        with _lock:
            _job["results"][server_id] = result
            _job["done"].append(server_id)

    with _lock:
        _job["running"] = False
        _job["current"] = None


def start(server_ids):
    with _lock:
        if _job["running"]:
            raise RuntimeError("замер уже идёт")
        _job.update({
            "running": True,
            "started_at": int(time.time()),
            "current": None,
            "queue": list(server_ids),
            "done": [],
            "results": {},
        })
    threading.Thread(target=_run, args=(list(server_ids),),
                     name="speedtest", daemon=True).start()
    return status()

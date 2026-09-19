"""Управление соседними контейнерами стека через docker.sock."""

import logging

import httpx

from . import settings

log = logging.getLogger("circle.docker")

_BASE = "http://docker/v1.44"
_client = None


def client():
    global _client
    if _client is None:
        _client = httpx.Client(
            transport=httpx.HTTPTransport(uds=settings.DOCKER_SOCKET),
            timeout=httpx.Timeout(30.0, connect=5.0),
        )
    return _client


def inspect(name):
    try:
        resp = client().get(f"{_BASE}/containers/{name}/json")
    except httpx.HTTPError as exc:
        return {"error": str(exc)}
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}"}
    data = resp.json()
    st = data.get("State") or {}
    health = (st.get("Health") or {}).get("Status")
    return {
        "name": (data.get("Name") or "").lstrip("/"),
        "status": st.get("Status"),
        "running": bool(st.get("Running")),
        "restarting": bool(st.get("Restarting")),
        "started_at": st.get("StartedAt"),
        "exit_code": st.get("ExitCode"),
        "restart_count": data.get("RestartCount", 0),
        "health": health,
        "image": (data.get("Config") or {}).get("Image"),
    }


def restart(name, timeout=10):
    try:
        resp = client().post(f"{_BASE}/containers/{name}/restart",
                             params={"t": timeout}, timeout=60)
    except httpx.HTTPError as exc:
        return False, str(exc)
    if resp.status_code in (204, 304):
        return True, ""
    return False, f"HTTP {resp.status_code}: {resp.text[:200]}"


def _demux(payload):
    """Docker отдаёт логи кадрами: 8 байт заголовка, потом тело."""
    chunks = []
    i = 0
    n = len(payload)
    while i + 8 <= n:
        if payload[i] not in (0, 1, 2):
            # Поток без TTY-мультиплексирования — отдаём как есть.
            return payload.decode("utf-8", "replace")
        size = int.from_bytes(payload[i + 4:i + 8], "big")
        i += 8
        chunks.append(payload[i:i + size])
        i += size
    return b"".join(chunks).decode("utf-8", "replace")


def logs(name, tail=200):
    try:
        resp = client().get(
            f"{_BASE}/containers/{name}/logs",
            params={"stdout": "1", "stderr": "1", "tail": str(tail), "timestamps": "0"},
        )
    except httpx.HTTPError as exc:
        return f"не удалось получить логи: {exc}"
    if resp.status_code != 200:
        return f"не удалось получить логи: HTTP {resp.status_code}"
    return _demux(resp.content)

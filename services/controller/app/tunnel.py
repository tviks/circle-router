"""Проверка живости самого туннеля, а не только процесса xray.

Watchdog и раньше снимал TPROXY, если xray упал. Но xray может работать
исправно, а сервер за ним — нет: нода перезагружается, меняется протокол,
провайдер начал резать соединение. Тогда весь LAN сидит без интернета.

Монитор раз в несколько секунд делает маленький HTTP-запрос через отдельный
служебный инбаунд xray, который всегда уходит в аутбаунд `proxy`. Несколько
провалов подряд — туннель объявляется мёртвым, watchdog снимает TPROXY, и
клиенты идут в интернет напрямую. Туннель ожил — VPN возвращается сам.
Проверка продолжается и в обходе: служебный инбаунд от TPROXY не зависит.
"""

import collections
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import state, xray_build

log = logging.getLogger("circle.tunnel")

HEALTH_PORT = xray_build.HEALTH_PORT


class TunnelMonitor:
    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tunnel-probe")
        self.history = collections.deque(maxlen=600)
        self.events = collections.deque(maxlen=200)
        self._reset("idle")

    def _reset(self, new_state, server_id=None):
        self.state = new_state
        self.server_id = server_id
        self.ok_streak = 0
        self.fail_streak = 0
        self.latency_ms = None
        self.error = None
        self.since = time.time()
        self.last_check = 0

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="tunnel", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()

    def poke(self):
        """Конфиг поменялся — проверить сразу, не дожидаясь интервала."""
        self.wake.set()

    def healthy(self):
        return self.state == "up"

    def snapshot(self):
        with self.lock:
            now = time.time()
            hour = [e for e in self.events if now - e["ts"] < 3600]
            recent = [p for p in self.history if now - p[0] < 300]
            lat = [p[1] for p in recent if p[1] is not None]
            return {
                "state": self.state,
                "since": int(self.since),
                "server_id": self.server_id,
                "latency_ms": self.latency_ms,
                "error": self.error,
                "last_check": int(self.last_check),
                "failovers_hour": sum(1 for e in hour if e["state"] == "down"),
                "loss_5m": round(1 - len(lat) / len(recent), 3) if recent else None,
                "avg_latency_5m": round(sum(lat) / len(lat), 1) if lat else None,
                "events": list(self.events)[-20:],
            }

    def series(self, seconds=900):
        with self.lock:
            now = time.time()
            return [{"ts": int(ts), "latency_ms": lat} for ts, lat in self.history
                    if now - ts < seconds]

    def failovers_since(self, ts, server_id=None):
        with self.lock:
            return sum(1 for e in self.events if e["ts"] >= ts and e["state"] == "down"
                       and (server_id is None or e.get("server_id") == server_id))

    def _probe_one(self, url, timeout):
        started = time.perf_counter()
        with httpx.Client(proxy=f"http://127.0.0.1:{HEALTH_PORT}", timeout=timeout) as client:
            resp = client.get(url)
        if resp.status_code not in (200, 204):
            raise RuntimeError(f"HTTP {resp.status_code}")
        return (time.perf_counter() - started) * 1000

    def _probe(self, urls, timeout):
        """Успех, если ответил хоть один адрес: один заблокированный на выходе
        сайт не должен отправлять весь дом в обход."""
        futures = [self.pool.submit(self._probe_one, url, timeout) for url in urls[:2]]
        best, error = None, None
        for future in futures:
            try:
                value = future.result(timeout=timeout + 2)
                best = value if best is None else min(best, value)
            except Exception as exc:                   # noqa: BLE001
                error = str(exc) or type(exc).__name__
        return best, error

    def _loop(self):
        while not self.stop_event.is_set():
            delay = 2.0
            try:
                delay = self._tick()
            except Exception:                          # noqa: BLE001
                log.exception("сбой в мониторе туннеля")
            self.wake.wait(delay)
            self.wake.clear()

    def _tick(self):
        cfg = state.load()
        failover = cfg.get("failover") or {}
        server = state.active_server(cfg)
        active = (bool(cfg.get("vpn_enabled")) and xray_build.effective()["vpn"]
                  and state.server_has_target(server))
        if not active:
            if self.state != "idle":
                with self.lock:
                    self._reset("idle")
            return 2.0

        server_id = server.get("id")
        if self.state == "idle" or server_id != self.server_id:
            with self.lock:
                self._reset("unknown", server_id)

        timeout = float(failover.get("timeout") or 4)
        urls = [u for u in failover.get("probe_urls") or [] if u] or \
            ["http://cp.cloudflare.com/generate_204", "http://www.gstatic.com/generate_204"]
        latency, error = self._probe(urls, timeout)

        with self.lock:
            now = time.time()
            self.last_check = now
            self.history.append((now, round(latency, 1) if latency is not None else None))
            if latency is not None:
                self.ok_streak += 1
                self.fail_streak = 0
                self.latency_ms = round(latency, 1)
                self.error = None
            else:
                self.fail_streak += 1
                self.ok_streak = 0
                self.error = error

            fail_limit = max(1, int(failover.get("fail_threshold") or 2))
            recover_limit = max(1, int(failover.get("recover_threshold") or 2))
            if self.state == "unknown" and self.ok_streak >= 1:
                self._transition("up", "туннель отвечает")
            elif self.state == "down" and self.ok_streak >= recover_limit:
                self._transition("up", "туннель снова отвечает — возвращаю VPN")
            elif self.state in ("up", "unknown") and self.fail_streak >= fail_limit:
                self._transition("down", f"туннель не отвечает ({error}) — клиенты идут напрямую")

        interval = float(failover.get("interval") or 3)
        return interval if self.state == "up" else 1.0

    def _transition(self, new_state, message):
        previous = self.state
        self.state = new_state
        self.since = time.time()
        # «unknown → up» — обычный старт, в журнал переключений не пишем.
        if not (previous == "unknown" and new_state == "up"):
            self.events.append({"ts": int(self.since), "state": new_state,
                                "server_id": self.server_id, "error": self.error})
        log.info("туннель: %s", message)


monitor = TunnelMonitor()

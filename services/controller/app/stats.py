"""Сэмплер метрик: счётчики интерфейсов, nftables-учёт по клиентам, статистика xray."""

import collections
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from . import db, firewall, settings, state, sysinfo
from .shell import out

log = logging.getLogger("circle.stats")

SAMPLE_INTERVAL = 2.0        # опрос интерфейсов
SLOW_EVERY = 3               # каждый N-й тик опрашиваем xray и nftables
LIVE_POINTS = 300            # ~10 минут живого графика
FLUSH_INTERVAL = 60          # раз в минуту скидываем дельты в SQLite

_STAT_RE = re.compile(r"^(inbound|outbound)>>>([^>]+)>>>traffic>>>(uplink|downlink)$")


def iface_counters(name):
    base = Path(f"/sys/class/net/{name}/statistics")
    try:
        return (int((base / "rx_bytes").read_text()),
                int((base / "tx_bytes").read_text()))
    except (OSError, ValueError):
        return None


def xray_stats():
    """{'proxy': {'up': n, 'down': n}, ...} по тегам аутбаундов."""
    raw = out([settings.XRAY_BIN, "api", "statsquery",
               f"--server={settings.XRAY_API_ADDR}", "-pattern="], timeout=6)
    if not raw:
        return {}
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    result = {}
    for item in doc.get("stat") or []:
        match = _STAT_RE.match(item.get("name", ""))
        if not match:
            continue
        kind, tag, direction = match.groups()
        key = f"{kind}:{tag}"
        bucket = result.setdefault(key, {"up": 0, "down": 0})
        bucket["up" if direction == "uplink" else "down"] += int(item.get("value") or 0)
    return result


def _delta(previous, current):
    """Разница счётчиков с защитой от их обнуления (рестарт xray, обрыв ppp0)."""
    if previous is None or current is None:
        return 0
    return current - previous if current >= previous else current


class Sampler:
    def __init__(self):
        self.live = collections.deque(maxlen=LIVE_POINTS)
        self.lock = threading.Lock()
        self.thread = None
        self.stop_event = threading.Event()
        self._prev_iface = {}
        self._prev_xray = {}
        self._prev_clients = {}
        self._pending = collections.defaultdict(int)
        self._pending_clients = collections.defaultdict(lambda: {"up": 0, "down": 0})
        self._last_flush = time.time()
        self._last_prune = 0.0
        self.client_rates = {}
        self._prev_cpu = {}
        self._prev_diskio = {}
        self.system = {}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="stats", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        db.init()
        tick = 0
        while not self.stop_event.wait(SAMPLE_INTERVAL):
            try:
                self._sample(tick)
            except Exception:                          # noqa: BLE001
                log.exception("сбой в цикле сбора метрик")
            tick += 1

    def _sample(self, tick):
        cfg = state.load()
        lan_if = cfg["lan"]["interface"]
        wan_if = state.wan_interface(cfg)
        now = time.time()

        sample = {"ts": int(now)}
        deltas = {}
        for role, iface in (("wan", wan_if), ("lan", lan_if)):
            counters = iface_counters(iface)
            key = f"{role}:{iface}"
            previous = self._prev_iface.get(key)
            if counters and previous:
                rx = _delta(previous[0], counters[0])
                tx = _delta(previous[1], counters[1])
            else:
                rx = tx = 0
            self._prev_iface[key] = counters
            deltas[f"{role}_rx"] = rx
            deltas[f"{role}_tx"] = tx
            sample[f"{role}_rx_rate"] = rx / SAMPLE_INTERVAL
            sample[f"{role}_tx_rate"] = tx / SAMPLE_INTERVAL

        if tick % SLOW_EVERY == 0:
            self._sample_xray(sample, deltas)
            self._sample_clients(now)
        else:
            last = self.live[-1] if self.live else {}
            for key in ("proxy_up_rate", "proxy_down_rate",
                        "direct_up_rate", "direct_down_rate"):
                sample[key] = last.get(key, 0.0)

        try:
            self._sample_system(tick)
        except Exception:                              # noqa: BLE001
            log.exception("не удалось собрать метрики железа")

        for key, value in deltas.items():
            self._pending[key] += value

        with self.lock:
            self.live.append(sample)

        if now - self._last_flush >= FLUSH_INTERVAL:
            self._flush(int(now))
            self._last_flush = now
        if now - self._last_prune > 6 * 3600:
            self._last_prune = now
            try:
                db.prune()
            except Exception:                          # noqa: BLE001
                log.exception("не удалось почистить историю")

    def _sample_system(self, tick):
        """Железо: процессор каждый тик, температуры реже, диски совсем редко.

        Занятость файловых систем меняется медленно, а statvfs по всем точкам
        монтирования — единственная относительно дорогая часть.
        """
        snapshot = dict(self.system)
        now = sysinfo.cpu_times()

        cores = []
        total_usage = None
        for name in sorted(now, key=lambda n: (n != "cpu", n)):
            idle, total = now[name]
            previous = self._prev_cpu.get(name)
            usage = 0.0
            if previous:
                idle_delta = idle - previous[0]
                total_delta = total - previous[1]
                if total_delta > 0:
                    usage = round(max(0.0, 100.0 * (1 - idle_delta / total_delta)), 1)
            if name == "cpu":
                total_usage = usage
            else:
                cores.append({"core": int(name[3:]), "usage": usage})
        self._prev_cpu = now

        snapshot["cpu"] = {
            "usage": total_usage if total_usage is not None else 0.0,
            "cores": cores,
            "loadavg": list(os.getloadavg()) if hasattr(os, "getloadavg") else [0, 0, 0],
            "frequency_mhz": snapshot.get("cpu", {}).get("frequency_mhz"),
            "model": snapshot.get("cpu", {}).get("model") or sysinfo.cpu_model(),
        }
        snapshot["memory"] = sysinfo.memory()

        if tick % SLOW_EVERY == 0:
            sensors = sysinfo.temperatures()
            snapshot["sensors"] = sensors
            snapshot["cpu"]["temperature"] = sysinfo.cpu_temperature(sensors)
            snapshot["cpu"]["frequency_mhz"] = sysinfo.cpu_frequency_mhz()
        else:
            snapshot["cpu"]["temperature"] = (self.system.get("cpu") or {}).get("temperature")

        # Скорость дисков — по дельте /proc/diskstats, сектор всегда 512 байт.
        current_io = sysinfo.diskstats()
        io_rates = {}
        for device, values in current_io.items():
            previous = self._prev_diskio.get(device)
            if previous:
                window = SAMPLE_INTERVAL
                io_rates[device] = {
                    "read": max(0, values[0] - previous[0]) * 512 / window,
                    "write": max(0, values[1] - previous[1]) * 512 / window,
                    "busy": min(100.0, round(max(0, values[2] - previous[2])
                                             / (window * 10), 1)),
                }
        self._prev_diskio = current_io
        snapshot["disk_io"] = io_rates

        if tick % 30 == 0 or "disks" not in snapshot:
            snapshot["disks"] = sysinfo.disks(snapshot.get("sensors"))

        snapshot["ts"] = int(time.time())
        self.system = snapshot

    def _sample_xray(self, sample, deltas):
        current = xray_stats()
        window = SAMPLE_INTERVAL * SLOW_EVERY
        for tag, out_key in (("outbound:proxy", "proxy"), ("outbound:direct", "direct")):
            now_values = current.get(tag, {"up": 0, "down": 0})
            previous = self._prev_xray.get(tag)
            up = _delta(previous["up"], now_values["up"]) if previous else 0
            down = _delta(previous["down"], now_values["down"]) if previous else 0
            self._prev_xray[tag] = dict(now_values)
            deltas[f"{out_key}_up"] = up
            deltas[f"{out_key}_down"] = down
            sample[f"{out_key}_up_rate"] = up / window
            sample[f"{out_key}_down_rate"] = down / window

    def _sample_clients(self, now):
        current = firewall.client_counters()
        window = SAMPLE_INTERVAL * SLOW_EVERY
        rates = {}
        for ip, values in current.items():
            previous = self._prev_clients.get(ip)
            up = _delta(previous["up"], values["up"]) if previous else 0
            down = _delta(previous["down"], values["down"]) if previous else 0
            rates[ip] = {"up_rate": up / window, "down_rate": down / window,
                         "up_total": values["up"], "down_total": values["down"]}
            if up or down:
                self._pending_clients[ip]["up"] += up
                self._pending_clients[ip]["down"] += down
        self._prev_clients = current
        self.client_rates = rates

    def _flush(self, ts):
        minute = ts - (ts % 60)
        values = {key: int(self._pending.get(key, 0)) for key in
                  ("wan_rx", "wan_tx", "lan_rx", "lan_tx",
                   "proxy_up", "proxy_down", "direct_up", "direct_down")}
        try:
            db.record_traffic(minute, values)
            db.record_clients(minute, dict(self._pending_clients))
        except Exception:                              # noqa: BLE001
            log.exception("не удалось записать метрики в БД")
            return
        self._pending.clear()
        self._pending_clients.clear()

    def snapshot(self, points=120):
        with self.lock:
            data = list(self.live)[-points:]
        return data

    def current_rates(self):
        with self.lock:
            return dict(self.live[-1]) if self.live else {}

    def flush_now(self):
        self._flush(int(time.time()))
        self._last_flush = time.time()


sampler = Sampler()

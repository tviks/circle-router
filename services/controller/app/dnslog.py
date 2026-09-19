"""Счётчики DNS-запросов и блокировок: читаем поток логов dnsmasq.

dnsmasq пишет в stderr, а stderr забирает docker — значит, ротацию берёт на себя
json-file драйвер, и нам не нужно самим следить за размером файла.

Формат строк (при log-queries=extra):
    query[A] ads.example.com from 192.168.10.50
    config ads.example.com is 0.0.0.0        <- ответ из нашего blocklist.conf
"""

import collections
import logging
import re
import threading
import time

import httpx

from . import settings

log = logging.getLogger("circle.dnslog")

_QUERY_RE = re.compile(r"query\[[A-Z0-9]+\]\s+(\S+)\s+from\s+(\S+)")
_BLOCK_RE = re.compile(r"config\s+(\S+)\s+is\s+(?:0\.0\.0\.0|NXDOMAIN|::)")

TOP_LIMIT = 15
_TRIM_AT = 4000          # выше этого числа ключей счётчики подрезаются
_TRIM_TO = 1500


class DnsLogReader:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread = None
        self.stop_event = threading.Event()
        self.started_at = 0.0
        self.queries = 0
        self.blocked = 0
        self.by_domain = collections.Counter()
        self.by_client = collections.Counter()
        self.blocked_by_client = collections.Counter()
        self.recent = collections.deque(maxlen=200)
        self.connected = False

    # ── жизненный цикл ──────────────────────────────────────────────────────

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.started_at = time.time()
        self.thread = threading.Thread(target=self._loop, name="dnslog", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def reset(self):
        with self.lock:
            self.queries = 0
            self.blocked = 0
            self.by_domain.clear()
            self.by_client.clear()
            self.blocked_by_client.clear()
            self.recent.clear()
            self.started_at = time.time()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self._stream()
            except Exception as exc:                   # noqa: BLE001
                log.debug("поток логов dnsmasq оборвался: %s", exc)
            self.connected = False
            # Контейнер мог перезапускаться — подождём и подключимся заново.
            if self.stop_event.wait(5):
                return

    def _stream(self):
        url = f"http://docker/v1.44/containers/{settings.DNSMASQ_CONTAINER}/logs"
        params = {"follow": "1", "stdout": "1", "stderr": "1", "tail": "0",
                  "since": str(int(time.time()))}
        transport = httpx.HTTPTransport(uds=settings.DOCKER_SOCKET)
        with httpx.Client(transport=transport, timeout=None) as client:
            with client.stream("GET", url, params=params) as resp:
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                self.connected = True
                for line in _demux_lines(resp.iter_bytes(), self.stop_event):
                    self._ingest(line)
                    if self.stop_event.is_set():
                        return

    # ── разбор ──────────────────────────────────────────────────────────────

    def _ingest(self, line):
        match = _QUERY_RE.search(line)
        if match:
            domain, client = match.group(1).lower(), match.group(2)
            with self.lock:
                self.queries += 1
                self.by_client[client] += 1
                self._trim()
            return

        match = _BLOCK_RE.search(line)
        if match:
            domain = match.group(1).lower()
            with self.lock:
                self.blocked += 1
                self.by_domain[domain] += 1
                self.recent.appendleft({"domain": domain, "ts": int(time.time())})
                self._trim()

    def _trim(self):
        """Держим счётчики в разумных рамках: сеть может нагенерить десятки
        тысяч уникальных доменов, а в UI видно только верхушку."""
        for counter in (self.by_domain, self.by_client, self.blocked_by_client):
            if len(counter) > _TRIM_AT:
                for key, _ in counter.most_common()[_TRIM_TO:]:
                    del counter[key]

    # ── наружу ──────────────────────────────────────────────────────────────

    def snapshot(self):
        with self.lock:
            queries, blocked = self.queries, self.blocked
            top_domains = self.by_domain.most_common(TOP_LIMIT)
            top_clients = self.by_client.most_common(TOP_LIMIT)
            recent = list(self.recent)[:30]
        share = (blocked / queries * 100) if queries else 0.0
        return {
            "connected": self.connected,
            "since": int(self.started_at),
            "queries": queries,
            "blocked": blocked,
            "blocked_share": round(share, 1),
            "top_blocked": [{"domain": d, "count": c} for d, c in top_domains],
            "top_clients": [{"client": c, "count": n} for c, n in top_clients],
            "recent_blocked": recent,
        }


def _demux_lines(chunks, stop_event):
    """Docker отдаёт логи кадрами: 8 байт заголовка, затем тело кадра."""
    buffer = bytearray()
    text = bytearray()
    for chunk in chunks:
        if stop_event.is_set():
            return
        buffer.extend(chunk)
        while len(buffer) >= 8:
            if buffer[0] not in (0, 1, 2):
                # Поток без мультиплексирования (TTY) — отдаём как есть.
                text.extend(buffer)
                buffer.clear()
                break
            size = int.from_bytes(buffer[4:8], "big")
            if len(buffer) < 8 + size:
                break
            text.extend(buffer[8:8 + size])
            del buffer[:8 + size]

        while b"\n" in text:
            line, _, rest = bytes(text).partition(b"\n")
            text = bytearray(rest)
            yield line.decode("utf-8", "replace")


reader = DnsLogReader()

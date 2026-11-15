"""Метрики своих нод: состояние сервера и качество линка до него.

На сервере ничего не ставится. Роутер держит одно SSH-подключение, в нём
крутится цикл из `cat /proc/...` раз в пять секунд, а всё остальное —
проценты, скорости, дельты — считается уже здесь. Стоит это серверу пары
форков `cat` в пять секунд.

Пока туннель жив, SSH идёт внутри него (через служебный инбаунд xray):
снаружи видно только VPN-соединение, без отдельной SSH-сессии к тому же IP.
Туннель упал — переподключаемся напрямую, метрики не пропадают как раз
тогда, когда они нужнее всего.
"""

import collections
import logging
import socket
import statistics
import threading
import time

from . import node_build as nb, ssh, state, tunnel, xray_build

log = logging.getLogger("circle.nodemon")

PERIOD = 5
HISTORY = 180        # 15 минут при опросе раз в 5 секунд
RTT_EVERY = 30

STREAM = r"""
P="%(ports)s"
IF=$(awk '$2=="00000000"{print $1; exit}' /proc/net/route)
while :; do
  echo "@@ $(date +%%s) $IF"
  head -n1 /proc/stat
  echo "LA $(cat /proc/loadavg)"
  echo "UP $(cat /proc/uptime)"
  grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo
  grep "^ *$IF:" /proc/net/dev | sed 's/^ */NET /'
  pid=$(systemctl show -p MainPID --value %(service)s 2>/dev/null)
  if [ -n "$pid" ] && [ "$pid" != 0 ] && [ -r /proc/$pid/stat ]; then
    echo "XS $(cut -d' ' -f14,15 /proc/$pid/stat)"
    echo "XR $(awk '/^VmRSS/{print $2}' /proc/$pid/status)"
  else
    echo "XS -"
  fi
  echo "CN $(cat /proc/net/tcp /proc/net/tcp6 2>/dev/null | awk -v ports="$P" 'BEGIN{n=split(ports,a," ");for(i=1;i<=n;i++)m[a[i]]=1} $4=="01"{split($2,b,":"); if(b[2] in m) c++} END{print c+0}')"
  echo "NP $(nproc 2>/dev/null || echo 1)"
  echo "@@end"
  sleep %(period)d
done
"""

CLK_TCK = 100


def _parse(lines):
    data = {}
    for line in lines:
        if line.startswith("@@ "):
            parts = line.split()
            data["ts"] = int(parts[1])
            data["iface"] = parts[2] if len(parts) > 2 else ""
        elif line.startswith("cpu "):
            values = [int(v) for v in line.split()[1:]]
            data["cpu_total"] = sum(values[:8])
            data["cpu_idle"] = values[3] + (values[4] if len(values) > 4 else 0)
        elif line.startswith("LA "):
            data["load"] = [float(v) for v in line.split()[1:4]]
        elif line.startswith("UP "):
            data["uptime"] = float(line.split()[1])
        elif line.split(":")[0] in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            key, value = line.split(":", 1)
            data[key] = int(value.split()[0]) * 1024
        elif line.startswith("NET "):
            _, rest = line[4:].split(":", 1)
            fields = rest.split()
            data["rx"] = int(fields[0])
            data["tx"] = int(fields[8])
        elif line.startswith("XS "):
            parts = line.split()[1:]
            if parts and parts[0] != "-":
                data["xray_ticks"] = int(parts[0]) + int(parts[1])
        elif line.startswith("XR "):
            value = line.split()[1:] or ["0"]
            data["xray_rss"] = int(value[0] or 0) * 1024
        elif line.startswith("CN "):
            data["connections"] = int(line.split()[1])
        elif line.startswith("NP "):
            data["cpus"] = int(line.split()[1])
    return data


def tcp_rtt(host, port, timeout=3.0):
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.perf_counter() - started) * 1000, 1)
    except OSError:
        return None


class Worker:
    def __init__(self, node_id):
        self.node_id = node_id
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name=f"nodemon-{node_id}", daemon=True)
        self.lock = threading.Lock()
        self.latest = {"online": False}
        self.history = collections.deque(maxlen=HISTORY)
        self.rtts = collections.deque(maxlen=20)
        self.rtt = None
        self._last_rtt = 0.0
        self._prev = None

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def snapshot(self):
        with self.lock:
            return dict(self.latest), list(self.history)

    def _loop(self):
        backoff = 5
        while not self.stop_event.is_set():
            try:
                self._stream()
                backoff = 5
            except Exception as exc:                   # noqa: BLE001
                with self.lock:
                    self.latest.update({"online": False, "error": str(exc)[:200],
                                        "ts": int(time.time())})
                log.debug("метрики ноды %s: %s", self.node_id, exc)
            if self.stop_event.wait(backoff):
                return
            backoff = min(backoff * 2, 60)

    def _via_tunnel(self, cfg, node):
        return (node.get("server_id") and cfg.get("active_server") == node["server_id"]
                and tunnel.monitor.healthy())

    def _stream(self):
        cfg = state.load()
        node = next((n for n in cfg.get("nodes") or [] if n["id"] == self.node_id), None)
        if not node:
            self.stop_event.set()
            return
        profile = node.get("profile") or {}
        ports = []
        if profile and nb.TRANSPORTS[profile["transport"]]["tcp"]:
            ports.append(f"{int(profile['port']):04X}")
        via = xray_build.HEALTH_PORT if self._via_tunnel(cfg, node) else None
        try:
            session = ssh.Session(node, via_proxy_port=via)
        except ssh.SshError:
            if not via:
                raise
            session = ssh.Session(node)
            via = None
        with session:
            channel = session.open_stream(STREAM % {"ports": " ".join(ports),
                                                    "service": nb.SERVICE, "period": PERIOD})
            channel.settimeout(PERIOD * 4)
            buffer = ""
            block = []
            with self.lock:
                self.latest.update({"online": True, "error": None, "via_tunnel": bool(via)})
            while not self.stop_event.is_set():
                chunk = channel.recv(65536)
                if not chunk:
                    raise ssh.SshError("сервер закрыл поток метрик")
                buffer += chunk.decode("utf-8", "replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line == "@@end":
                        self._ingest(node, profile, _parse(block), bool(via))
                        block = []
                    else:
                        block.append(line)
                # Туннель ожил, а мы сидим напрямую (или наоборот) —
                # переподключимся правильным путём.
                if bool(via) != bool(self._via_tunnel(state.load(), node)):
                    return

    def _ingest(self, node, profile, data, via):
        now = time.time()
        # Пустое TCP-соединение к порту ноды раз в 30 секунд, не чаще: частые
        # «подключился и молчит» — сами по себе странный рисунок трафика.
        if now - self._last_rtt >= RTT_EVERY:
            self._last_rtt = now
            if profile and nb.TRANSPORTS[profile["transport"]]["tcp"]:
                self.rtt = tcp_rtt(node["host"], int(profile["port"]))
            else:
                self.rtt = tcp_rtt(node["host"], int(node.get("ssh_port") or 22))
            self.rtts.append(self.rtt)
        rtt = self.rtt
        ok = [r for r in self.rtts if r is not None]

        prev, self._prev = self._prev, (now, data)
        cpu = xray_cpu = rx_rate = tx_rate = None
        if prev:
            dt = max(now - prev[0], 0.5)
            p = prev[1]
            if "cpu_total" in p and "cpu_total" in data and data["cpu_total"] > p["cpu_total"]:
                busy = (data["cpu_total"] - p["cpu_total"]) - (data["cpu_idle"] - p["cpu_idle"])
                cpu = round(100 * busy / (data["cpu_total"] - p["cpu_total"]), 1)
            if "xray_ticks" in p and "xray_ticks" in data and data["xray_ticks"] >= p["xray_ticks"]:
                xray_cpu = round(100 * (data["xray_ticks"] - p["xray_ticks"]) / CLK_TCK / dt
                                 / max(data.get("cpus", 1), 1), 2)
            if "rx" in p and "rx" in data and data["rx"] >= p["rx"]:
                rx_rate = (data["rx"] - p["rx"]) / dt
                tx_rate = (data["tx"] - p["tx"]) / dt

        mem_total = data.get("MemTotal", 0)
        mem_used = mem_total - data.get("MemAvailable", 0)
        snapshot = {
            "online": True, "error": None, "via_tunnel": via, "ts": int(now),
            "cpu": cpu, "cpus": data.get("cpus"), "load": data.get("load"),
            "uptime": data.get("uptime"),
            "mem_total": mem_total, "mem_used": mem_used,
            "mem_pct": round(100 * mem_used / mem_total, 1) if mem_total else None,
            "swap_total": data.get("SwapTotal", 0),
            "swap_used": data.get("SwapTotal", 0) - data.get("SwapFree", 0),
            "iface": data.get("iface"), "rx_rate": rx_rate, "tx_rate": tx_rate,
            "rx_total": data.get("rx"), "tx_total": data.get("tx"),
            "xray_running": "xray_ticks" in data, "xray_cpu": xray_cpu,
            "xray_rss": data.get("xray_rss"), "connections": data.get("connections"),
            "rtt_ms": rtt,
            "rtt_avg": round(statistics.mean(ok), 1) if ok else None,
            "rtt_jitter": round(statistics.pstdev(ok), 1) if len(ok) > 1 else None,
            "rtt_loss": round(1 - len(ok) / len(self.rtts), 3) if self.rtts else None,
        }
        with self.lock:
            self.latest = snapshot
            self.history.append({k: snapshot[k] for k in
                                 ("ts", "cpu", "mem_pct", "rx_rate", "tx_rate",
                                  "rtt_ms", "connections", "xray_cpu")})


class NodeMonitor:
    """Держит по воркеру на каждую готовую ноду."""

    def __init__(self):
        self.workers = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="nodemon", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        with self.lock:
            for worker in self.workers.values():
                worker.stop()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self._sync()
            except Exception:                          # noqa: BLE001
                log.exception("сбой в мониторе нод")
            self.stop_event.wait(10)

    def _sync(self):
        cfg = state.load()
        wanted = {n["id"] for n in cfg.get("nodes") or []
                  if n.get("status") == "ready" and ssh.key_path(n["id"]).exists()}
        with self.lock:
            for node_id in list(self.workers):
                if node_id not in wanted or not self.workers[node_id].thread.is_alive():
                    self.workers.pop(node_id).stop()
            for node_id in wanted - set(self.workers):
                worker = Worker(node_id)
                self.workers[node_id] = worker
                worker.start()

    def restart(self, node_id):
        """Профиль сменился — порт для подсчёта соединений тоже."""
        with self.lock:
            worker = self.workers.pop(node_id, None)
        if worker:
            worker.stop()

    def snapshot(self, node_id):
        with self.lock:
            worker = self.workers.get(node_id)
        if not worker:
            return {"online": False}, []
        return worker.snapshot()


monitor = NodeMonitor()

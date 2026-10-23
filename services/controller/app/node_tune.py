"""Подбор протокола и стратегии обхода под текущий аплинк.

Схема: на ноде поднимается временный экземпляр ядра с несколькими пробными
инбаундами, на роутере — временный xray, где у каждого кандидата свой
локальный HTTP-инбаунд и свой аутбаунд. Дальше всё меряется с роутера, то
есть ровно через того провайдера, через которого потом пойдёт трафик.

Раунды:
  A. транспорты — Vision, XHTTP, gRPC, SS-2022, Hysteria2 на лучшем SNI;
  B. маскировочные домены — Vision/XHTTP на остальных SNI;
  C. стратегии клиента — фрагментация ClientHello и другие uTLS-отпечатки
     для TCP-транспортов (то, что спасает, когда DPI режет по SNI или по
     отпечатку);
  D. финал — скорость и стабильность у лучших и у текущего боевого профиля.

Итоговая оценка: доля успешных подключений, скорость, задержка. Кандидат с
провалами больше трети не рассматривается вовсе, как бы быстр он ни был.
"""

import json
import logging
import socket
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from . import node_build as nb
from . import node_remote, settings, xray_build

log = logging.getLogger("circle.tune")

PROBE_URLS = ["http://cp.cloudflare.com/generate_204",
              "http://www.gstatic.com/generate_204"]
SPEED_URL = "https://speed.cloudflare.com/__down?bytes=25000000"
MIN_SUCCESS = 0.67
FINALISTS = 5


# ── локальный тестер ─────────────────────────────────────────────────────────

def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_port(port, timeout=10.0):
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


class Tester:
    """Временный xray на роутере: по HTTP-инбаунду на каждого кандидата.

    Каждый кандидат сначала проверяется `xray run -test` поодиночке: один
    неподдерживаемый вариант (например, Hysteria2 на старом ядре) не должен
    валить весь раунд.
    """

    def __init__(self, node, cands):
        self.node = node
        self.cands = cands
        self.ports = {}
        self.invalid = {}
        self.process = None
        self.path = None

    def _pair(self, cand, index):
        outbound, extras = nb.client_outbounds(self.node, cand)
        return nb.retag(outbound, extras, f"c{index}")

    def __enter__(self):
        inbounds, outbounds, rules = [], [], []
        for index, cand in enumerate(self.cands):
            key = nb.cand_key(cand)
            if key in self.ports or key in self.invalid:
                continue
            outbound, extras = self._pair(cand, index)
            port = _free_port()
            single = {
                "log": {"loglevel": "error"},
                "inbounds": [{"tag": "t", "listen": "127.0.0.1", "port": port, "protocol": "http"}],
                "outbounds": [outbound, *extras],
            }
            check = settings.XRAY_CONFIG_PATH.parent / f"tune-check-{index}.json"
            check.write_text(json.dumps(single), encoding="utf-8")
            ok, output = xray_build.validate(check)
            check.unlink(missing_ok=True)
            if not ok:
                self.invalid[key] = xray_build._first_error_line(output)
                continue
            tag = f"in{index}"
            inbounds.append({"tag": tag, "listen": "127.0.0.1", "port": port, "protocol": "http"})
            outbounds.extend([outbound, *extras])
            rules.append({"type": "field", "inboundTag": [tag], "outboundTag": outbound["tag"]})
            self.ports[key] = port

        if not self.ports:
            return self
        outbounds.append({"tag": "block", "protocol": "blackhole"})
        config = {"log": {"loglevel": "error"}, "inbounds": inbounds, "outbounds": outbounds,
                  "routing": {"rules": rules}}
        self.path = settings.XRAY_CONFIG_PATH.parent / f"tune-{_free_port()}.json"
        self.path.write_text(json.dumps(config), encoding="utf-8")
        self.process = subprocess.Popen(
            [settings.XRAY_BIN, "run", "-config", str(self.path), "-format=json"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not all(_wait_port(p) for p in self.ports.values()):
            self.__exit__(None, None, None)
            raise RuntimeError("локальный xray для замеров не поднялся")
        return self

    def __exit__(self, *exc):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
        if self.path:
            self.path.unlink(missing_ok=True)


def connectivity(port, attempts=3, timeout=6.0):
    """Несколько запросов, каждый новым соединением: проверяется и handshake
    с сервером, а не только уже открытый поток."""
    latencies, errors = [], []
    proxy = f"http://127.0.0.1:{port}"
    for i in range(attempts):
        url = PROBE_URLS[i % len(PROBE_URLS)]
        started = time.perf_counter()
        try:
            with httpx.Client(proxy=proxy, timeout=timeout) as client:
                resp = client.get(url)
            if resp.status_code in (200, 204):
                latencies.append((time.perf_counter() - started) * 1000)
            else:
                errors.append(f"HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            errors.append(type(exc).__name__)
    result = {"ok": len(latencies), "total": attempts,
              "success": round(len(latencies) / attempts, 2)}
    if latencies:
        result["latency_ms"] = round(statistics.median(latencies), 1)
        result["jitter_ms"] = round(statistics.pstdev(latencies), 1) if len(latencies) > 1 else 0.0
    if errors:
        result["error"] = errors[-1]
    return result


def throughput(port, duration=5, stall_after=5.0):
    """Скорость через кандидата. Отдельно ловим «заморозку»: соединение живо,
    а данные перестают идти после первых десятков килобайт. Так выглядит
    фильтр, который режет TLS-сессии к хостингам после ~16–20 КБ."""
    proxy = f"http://127.0.0.1:{port}"
    timeout = httpx.Timeout(connect=8.0, read=stall_after, write=8.0, pool=8.0)
    downloaded = 0
    started = None
    try:
        with httpx.Client(proxy=proxy, timeout=timeout) as client:
            for _ in range(20):
                with client.stream("GET", SPEED_URL) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(65536):
                        if started is None:
                            started = time.perf_counter()
                        downloaded += len(chunk)
                        if time.perf_counter() - started >= duration:
                            break
                if started and time.perf_counter() - started >= duration:
                    break
    except httpx.HTTPError as exc:
        if downloaded and downloaded < 256 * 1024:
            return {"mbps": 0.0, "bytes": downloaded, "stall": True,
                    "error": f"поток замер после {downloaded // 1024} КБ — похоже на фильтр DPI"}
        if not downloaded:
            return {"mbps": 0.0, "bytes": 0, "stall": False, "error": type(exc).__name__}
    if not started:
        return {"mbps": 0.0, "bytes": 0, "stall": False, "error": "не пришло ни байта"}
    elapsed = max(time.perf_counter() - started, 0.001)
    return {"mbps": round(downloaded * 8 / elapsed / 1_000_000, 2),
            "bytes": downloaded, "seconds": round(elapsed, 2), "stall": False}


def score(result):
    """Одно число для сравнения: 0 — негоден, None — скорость не мерили."""
    success = result.get("success", 0)
    if success < MIN_SUCCESS or result.get("stall"):
        return 0.0
    if result.get("mbps") is None:
        return None
    latency = result.get("latency_ms") or 1000
    jitter = result.get("jitter_ms") or 0
    value = result["mbps"] * success ** 2 / (1 + latency / 400) / (1 + jitter / 300)
    return round(value, 2)


# ── раунды ───────────────────────────────────────────────────────────────────

class Tuner:
    def __init__(self, node, session, report, speed_seconds=5, production=None,
                 finalists=FINALISTS):
        self.node = node
        self.session = session
        self.report = report            # report(message, progress 0..1)
        self.speed_seconds = speed_seconds
        self.finalists = finalists
        self.production = production    # кандидат, который сейчас в бою
        self.results = {}               # cand_key -> результат
        self.opened = set()
        self.live_specs = []
        self.skip = set()               # транспорты, которые сервер не принял
        self.sni_order = []
        self.busy = set()
        if production:
            self.busy.add((nb.network_of(production), int(production["port"])))

    def _record(self, cand, measurement, round_name):
        key = nb.cand_key(cand)
        entry = self.results.setdefault(key, {"key": key, "cand": cand,
                                              "label": nb.describe(cand)})
        # Один и тот же кандидат меряется в нескольких раундах: попытки
        # копятся, а не затирают друг друга. Иначе один шумный раунд
        # перечёркивал бы чистый результат предыдущего.
        if "total" in measurement and "total" in entry:
            ok = entry["ok"] + measurement["ok"]
            total = entry["total"] + measurement["total"]
            measurement = {**measurement, "ok": ok, "total": total,
                           "success": round(ok / total, 2)}
            if measurement.get("latency_ms") is None:
                measurement.pop("latency_ms", None)
                measurement.pop("jitter_ms", None)
        if "error" not in measurement:
            entry.pop("error", None)
        entry.update(measurement)
        entry["round"] = round_name
        entry["current"] = bool(self.production) and key == nb.cand_key(self.production)
        return entry

    def _probe_round(self, specs, cands, round_name, attempts=3):
        """Поднять пробу с этими инбаундами и прогнать кандидатов."""
        needed = {(nb.network_of(s), int(s["port"])) for s in specs}
        fresh = needed - self.opened
        if fresh:
            node_remote.open_ports(self.session, sorted(fresh))
            self.opened |= fresh
        if specs and [nb.spec_key(s) for s in specs] != self.live_specs:
            try:
                node_remote.start_probe(self.session, self.node, specs)
            except RuntimeError as exc:
                if not nb.uses_hy2(specs):
                    raise
                # Hysteria2 в ядре молодой: если сервер его не принял, не
                # теряем весь раунд, а повторяем без него.
                self.report(f"Hysteria2 не поднялся на сервере ({exc}), продолжаю без него", None)
                for c in [c for c in cands if c["transport"] == "hy2"]:
                    self._record(c, {"success": 0, "error": "сервер не принял"}, round_name)
                self.skip.add("hy2")
                specs = [s for s in specs if s["transport"] != "hy2"]
                cands = [c for c in cands if c["transport"] != "hy2"]
                node_remote.start_probe(self.session, self.node, specs)
            self.live_specs = [nb.spec_key(s) for s in specs]
        if self.production and round_name in ("A", "D"):
            cands = [*cands, self.production]

        with Tester(self.node, cands) as tester:
            for key, error in tester.invalid.items():
                cand = next(c for c in cands if nb.cand_key(c) == key)
                self._record(cand, {"success": 0, "error": f"ядро роутера: {error}"}, round_name)
            if not tester.ports:
                return
            by_key = {nb.cand_key(c): c for c in cands}
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {key: pool.submit(connectivity, port, attempts)
                           for key, port in tester.ports.items()}
                first = {key: future.result() for key, future in futures.items()}
            for key, measurement in first.items():
                self._record(by_key[key], measurement, round_name)
            # Параллельные замеры мешают друг другу. У кого были отказы —
            # перепроверяем по одному, чтобы не выкинуть рабочий вариант
            # из-за толкотни на собственном канале.
            for key, measurement in first.items():
                if measurement["success"] < 1:
                    self._record(by_key[key], connectivity(tester.ports[key], attempts), round_name)
            if round_name == "D":
                self._final(tester, by_key)

    def _final(self, tester, by_key):
        alive = [self.results[k] for k in tester.ports
                 if self.results.get(k, {}).get("success", 0) >= MIN_SUCCESS]
        alive.sort(key=lambda r: r.get("latency_ms") or 9999)
        for i, entry in enumerate(alive):
            self.report(f"Скорость и стабильность: {entry['label']}",
                        0.75 + 0.2 * i / max(len(alive), 1))
            port = tester.ports[entry["key"]]
            speed = throughput(port, self.speed_seconds)
            # Ещё серия новых соединений — разброс задержки и доля отказов
            # честнее на шести попытках, чем на трёх из первого раунда.
            stability = connectivity(port, attempts=6)
            self._record(entry["cand"], {**speed, **stability}, "D")

    def _best(self, predicate):
        """Лучший рабочий вариант. При прочих равных — самый простой.

        Задержки в раундах меряются параллельно и потому шумные, так что
        усложнения (фрагментация, нестандартный отпечаток, запасной SNI)
        должны выигрывать с запасом, а не на погрешности: фрагментация ещё и
        сама по себе добавляет пару десятков миллисекунд на handshake.
        """
        good = [r for r in self.results.values()
                if predicate(r["cand"]) and r.get("success", 0) >= MIN_SUCCESS]

        def weight(r):
            c = r["cand"]
            penalty = 1.0
            if c.get("frag"):
                penalty += 0.25
            if c.get("fp") not in (None, "chrome"):
                penalty += 0.1
            if c.get("sni") in self.sni_order:
                penalty += 0.1 * self.sni_order.index(c["sni"])
            return (-r["success"], (r.get("latency_ms") or 9999) * penalty)

        good.sort(key=weight)
        return good[0]["cand"] if good else None

    def run(self, sni_candidates):
        try:
            return self._run(sni_candidates)
        finally:
            try:
                node_remote.stop_probe(self.session)
            finally:
                keep = set(self.busy)
                node_remote.close_ports(self.session, sorted(self.opened - keep))

    def _run(self, sni_candidates):
        node = self.node
        self.report("Проверяю маскировочные домены с сервера", 0.05)
        sni_report = node_remote.check_sni(self.session, sni_candidates)
        good_sni = [r["domain"] for r in sorted(
            (r for r in sni_report if r["ok"]), key=lambda r: r.get("ms") or 9999)]
        if not good_sni:
            # Неизвестный формат вывода `tls ping` или все упали — проверим
            # вживую, это надёжнее.
            self.report("Сервер не подтвердил ни один домен, проверю их вживую", None)
            good_sni = list(sni_candidates)
        good_sni = good_sni[:3]
        self.sni_order = good_sni
        sni1 = good_sni[0]

        def spec(transport, sni=None, busy=None):
            port = nb.pick_port(node, transport, busy if busy is not None else self.busy)
            if port is None:
                return None
            return {"transport": transport, "port": port, "sni": sni if nb.TRANSPORTS[transport]["sni"] else None}

        def layout(transports_with_sni):
            busy = set(self.busy)
            specs = []
            for transport, sni in transports_with_sni:
                if transport in self.skip:
                    continue
                s = spec(transport, sni, busy)
                if s:
                    busy.add((nb.network_of(s), s["port"]))
                    specs.append(s)
            return specs

        def client(s, fp="chrome", frag=False):
            c = dict(s)
            if not nb.TRANSPORTS[s["transport"]].get("fixed_client"):
                c["fp"], c["frag"] = fp, frag
            return c

        # A. транспорты
        self.report("Раунд A: какие транспорты проходят через аплинк", 0.15)
        specs_a = layout([("vision", sni1), ("xhttp", sni1), ("grpc", sni1),
                          ("ss2022", None), ("hy2", None)])
        self._probe_round(specs_a, [client(s) for s in specs_a], "A")

        # B. домены
        for i, sni in enumerate(good_sni[1:], start=1):
            self.report(f"Раунд B: маскировка под {sni}", 0.15 + 0.15 * i / len(good_sni))
            specs_b = layout([("vision", sni), ("xhttp", sni)])
            self._probe_round(specs_b, [client(s) for s in specs_b], "B")

        # C. стратегии клиента на лучшем SNI каждого TCP-транспорта
        self.report("Раунд C: фрагментация ClientHello и uTLS-отпечатки", 0.4)
        best_sni = {}
        for transport in ("vision", "xhttp", "grpc"):
            cand = self._best(lambda c, t=transport: c["transport"] == t)
            best_sni[transport] = cand["sni"] if cand else sni1
        final_layout = layout([("vision", best_sni["vision"]), ("xhttp", best_sni["xhttp"]),
                               ("grpc", best_sni["grpc"]), ("ss2022", None), ("hy2", None)])
        variants = []
        for s in final_layout:
            if nb.TRANSPORTS[s["transport"]].get("fixed_client"):
                variants.append(client(s))
                continue
            variants.append(client(s, "chrome", False))
            variants.append(client(s, "chrome", True))
            for fp in nb.FINGERPRINTS[1:]:
                variants.append(client(s, fp, False))
        self._probe_round(final_layout, variants, "C")

        # D. финалисты: по одному лучшему варианту на транспорт, затем топ
        self.report("Раунд D: скорость и стабильность финалистов", 0.7)
        finalists = []
        for transport in nb.TRANSPORTS:
            cand = self._best(lambda c, t=transport: c["transport"] == t and
                              any(nb.spec_key(c) == nb.spec_key(s) for s in final_layout))
            if cand:
                finalists.append(cand)
        finalists.sort(key=lambda c: self.results[nb.cand_key(c)].get("latency_ms") or 9999)
        finalists = finalists[:self.finalists]
        if finalists:
            self._probe_round(final_layout, finalists, "D")
        elif self.production:
            self._probe_round([], [], "D")

        for entry in self.results.values():
            entry["score"] = score(entry)
        ranked = sorted(self.results.values(), key=lambda r: r.get("score") or 0, reverse=True)
        winner = ranked[0] if ranked and (ranked[0].get("score") or 0) > 0 else None
        current = self.results.get(nb.cand_key(self.production)) if self.production else None
        self.report("Подбор завершён", 1.0)
        return {
            "ts": int(time.time()),
            "sni": sni_report,
            "candidates": ranked,
            "winner": winner["cand"] if winner else None,
            "winner_key": winner["key"] if winner else None,
            "current_key": current["key"] if current else None,
        }


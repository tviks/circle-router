"""Свои VDS-ноды: развёртывание, подбор протокола, советник.

Жизненный цикл ноды:
  new → deploying → ready  (или error)

Деплой: SSH по паролю → ключ роутера в authorized_keys → ядро xray и
systemd-unit → полный подбор протокола → победитель становится боевым
конфигом ноды и сервером в списке VPN.

Советник раз в N часов (и сразу, если туннель за последний час несколько раз
падал) прогоняет подбор заново. Боевой экземпляр при этом не трогается:
кандидаты живут во временном. Нашлось заметно лучше — появляется
предложение в панели, а с галкой «применять сам» оно применяется сразу.
"""

import copy
import logging
import threading
import time

from . import apply, node_build as nb, node_monitor, node_remote, ssh, state, tunnel
from .node_tune import Tuner

log = logging.getLogger("circle.nodes")

LOG_LINES = 200
_jobs = {}
_jobs_lock = threading.Lock()

DEFAULT_TUNE = {
    "auto": True,             # перепроверять по расписанию и при сбоях
    "interval_hours": 24,
    "auto_apply": False,      # применять найденное без подтверждения
    "improve_pct": 25,        # насколько лучше должен быть кандидат
    "speed_seconds": 5,       # длительность замера скорости на кандидата
}


# ── состояние ────────────────────────────────────────────────────────────────

def find(cfg, node_id):
    return next((n for n in cfg.get("nodes") or [] if n.get("id") == node_id), None)


def get(node_id):
    node = find(state.load(), node_id)
    if not node:
        raise KeyError(node_id)
    return node


def update(node_id, mutate):
    result = {}

    def wrapper(cfg):
        node = find(cfg, node_id)
        if node is None:
            raise KeyError(node_id)
        mutate(node, cfg)
        result["node"] = copy.deepcopy(node)

    state.update(wrapper)
    return result["node"]


def job_of(node_id):
    with _jobs_lock:
        job = _jobs.get(node_id)
        return copy.deepcopy(job) if job else None


def any_running():
    with _jobs_lock:
        return any(j["running"] for j in _jobs.values())


def _trim_tune(last):
    if not last:
        return None
    keys = ("key", "label", "round", "success", "ok", "total", "latency_ms", "jitter_ms",
            "mbps", "stall", "error", "score", "current", "cand")
    return {
        "ts": last.get("ts"),
        "winner_key": last.get("winner_key"),
        "current_key": last.get("current_key"),
        "reason": last.get("reason"),
        "sni": last.get("sni"),
        "candidates": [{k: c.get(k) for k in keys} for c in last.get("candidates") or []],
    }


def public(node, cfg):
    """Без секретов: ключи Reality, пароли и сертификат наружу не уходят."""
    profile = node.get("profile")
    server_id = node.get("server_id")
    return {
        "id": node["id"],
        "name": node.get("name"),
        "host": node.get("host"),
        "ssh_port": node.get("ssh_port", 22),
        "ssh_user": node.get("ssh_user", "root"),
        "status": node.get("status"),
        "error": node.get("error"),
        "created": node.get("created"),
        "facts": node.get("facts") or {},
        "xray_version": node.get("xray_version"),
        "sni": node.get("sni") or [],
        "tune": {**DEFAULT_TUNE, **(node.get("tune") or {})},
        "profile": profile,
        "profile_label": nb.describe(profile) if profile else None,
        "server_id": server_id,
        "active": bool(server_id) and cfg.get("active_server") == server_id,
        "has_host_key": bool(node.get("host_key")),
        "last_tune": _trim_tune(node.get("last_tune")),
        "recommendation": node.get("recommendation"),
        "job": job_of(node["id"]),
    }


def create(payload):
    node = {
        "id": state.new_id(),
        "name": payload["name"],
        "host": payload["host"],
        "ssh_port": int(payload.get("ssh_port") or 22),
        "ssh_user": payload.get("ssh_user") or "root",
        "status": "new",
        "created": int(time.time()),
        "sni": payload.get("sni") or [],
        "tune": {**DEFAULT_TUNE, **(payload.get("tune") or {})},
        "activate": bool(payload.get("activate", True)),
        "secrets": nb.new_secrets(),
        "profile": None,
        "server_id": None,
        "host_key": None,
        "facts": {},
        "last_tune": None,
        "recommendation": None,
        "error": None,
    }
    state.update(lambda cfg: cfg.setdefault("nodes", []).append(node))
    return node


# ── задачи ───────────────────────────────────────────────────────────────────

class JobBusy(RuntimeError):
    pass


def start(node_id, kind, **kwargs):
    with _jobs_lock:
        # Один подбор за раз на весь роутер: замеры скорости делят аплинк.
        running = [j for j in _jobs.values() if j["running"]]
        if running:
            raise JobBusy(f"уже выполняется: {running[0]['title']}")
        job = {"kind": kind, "title": TITLES.get(kind, kind), "running": True,
               "started": int(time.time()), "finished": None, "progress": 0.0,
               "step": "", "log": [], "ok": None, "error": None}
        _jobs[node_id] = job
    threading.Thread(target=_run, args=(node_id, kind, kwargs),
                     name=f"node-{kind}", daemon=True).start()
    return job_of(node_id)


def _reporter(node_id):
    def report(message, progress=None):
        with _jobs_lock:
            job = _jobs.get(node_id)
            if not job:
                return
            job["step"] = message
            if progress is not None:
                job["progress"] = round(max(job["progress"], progress), 3)
            job["log"].append({"ts": int(time.time()), "text": message})
            del job["log"][:-LOG_LINES]
        log.info("нода %s: %s", node_id, message)
    return report


def _run(node_id, kind, kwargs):
    report = _reporter(node_id)
    ok, error = False, None
    try:
        JOBS[kind](node_id, report, **kwargs)
        ok = True
    except KeyError:
        error = "нода удалена"
    except Exception as exc:                           # noqa: BLE001
        error = str(exc) or type(exc).__name__
        log.exception("задача %s для ноды %s упала", kind, node_id)
        report(f"Ошибка: {error}")
        if kind == "deploy":
            try:
                update(node_id, lambda n, _: n.update({"status": "error", "error": error}))
            except KeyError:
                pass
    finally:
        with _jobs_lock:
            job = _jobs.get(node_id)
            if job:
                job.update({"running": False, "finished": int(time.time()),
                            "ok": ok, "error": error})
                if ok:
                    job["progress"] = 1.0


def _session(node, report, password=None, private_key=None):
    report(f"Подключаюсь к {node['host']}:{node.get('ssh_port', 22)}")
    session = ssh.Session(node, password=password, private_key=private_key)
    if session.learned_host_key and not node.get("host_key"):
        update(node["id"], lambda n, _: n.update({"host_key": session.learned_host_key}))
        node["host_key"] = session.learned_host_key
    return session


def _deploy(node_id, report, password=None, private_key=None):
    node = update(node_id, lambda n, _: n.update({"status": "deploying", "error": None}))
    report("Готовлю ключ доступа роутера", 0.01)
    pubkey = ssh.ensure_keypair(node_id)

    if password or private_key:
        with _session(node, report, password, private_key) as session:
            session.install_pubkey(pubkey)
        report("Ключ роутера добавлен, пароль больше не понадобится", 0.03)
    # Дальше — только по своему ключу: заодно убеждаемся, что он принят.
    with _session(node, report) as session:
        version = node_remote.router_xray_version()
        report(f"Ставлю ядро xray {version} и настраиваю систему", 0.05)
        facts, _ = node_remote.bootstrap(session, version)
        node = update(node_id, lambda n, _: n.update({"facts": facts, "xray_version": version}))
        report(f"Сервер: {facts.get('os', '?')}, {facts.get('arch', '?')}, "
               f"congestion control {facts.get('cc', '?')}", 0.1)
        node_remote.install_cert(session, node)
        _tune_and_settle(node, session, report, initial=True)
    update(node_id, lambda n, _: n.update({"status": "ready", "error": None}))
    report("Нода готова", 1.0)


def _tune(node_id, report, reason="вручную"):
    node = get(node_id)
    if node.get("status") != "ready":
        raise RuntimeError("нода ещё не развёрнута")
    with _session(node, report) as session:
        _tune_and_settle(node, session, report, initial=False, reason=reason)


def _tune_and_settle(node, session, report, initial, reason="первый деплой"):
    tune_cfg = {**DEFAULT_TUNE, **(node.get("tune") or {})}
    production = None if initial else node.get("profile")
    sni = [s for s in node.get("sni") or [] if s] or nb.DEFAULT_SNI
    # Плановая перепроверка экономит трафик: скорость меряем у трёх лучших,
    # а не у всех транспортов — на гигабите это сотни мегабайт за раз.
    scheduled = not initial and reason != "вручную"
    tuner = Tuner(node, session, lambda m, p=None: report(m, None if p is None else 0.1 + 0.8 * p),
                  speed_seconds=int(tune_cfg["speed_seconds"]), production=production,
                  finalists=3 if scheduled else 5)
    result = tuner.run(sni)
    result["reason"] = reason
    winner = result.get("winner")
    recommendation = _recommend(result, production, tune_cfg)
    previous = node.get("recommendation")
    if recommendation:
        # Сколько проверок подряд предлагают одно и то же. Замеры шумные:
        # автоприменение ждёт подтверждения, чтобы не прыгать туда-сюда.
        same = previous and previous.get("key") == recommendation["key"]
        recommendation["seen"] = (previous.get("seen", 1) + 1) if same else 1

    def save(n, _):
        n["last_tune"] = result
        n["recommendation"] = recommendation
    node = update(node["id"], save)

    if not winner and not production:
        raise RuntimeError("ни один протокол не прошёл через текущий аплинк — "
                           "сервер или его IP, похоже, заблокированы")
    if not winner:
        report("Ничего рабочего лучше текущего профиля не нашлось", 0.95)
        return

    if production is None:
        report(f"Лучший вариант: {nb.describe(winner)}", 0.92)
        _settle(node, session, winner, report)
    elif recommendation and tune_cfg.get("auto_apply") and (
            recommendation["seen"] >= 2 or recommendation["current_score"] <= 0):
        report(f"Применяю найденное автоматически: {recommendation['label']}", 0.92)
        _settle(node, session, recommendation["cand"], report)
    elif recommendation and tune_cfg.get("auto_apply"):
        report(f"Предложение: {recommendation['label']} — применю сам, если подтвердится "
               f"на повторной проверке через час", 0.95)
    elif recommendation:
        report(f"Предложение: {recommendation['label']} — {recommendation['reason']}", 0.95)
    else:
        report("Текущий профиль по-прежнему лучший", 0.95)


def _recommend(result, production, tune_cfg):
    """Предлагаем смену, если кандидат заметно лучше текущего или текущий
    вовсе перестал проходить."""
    if not production or not result.get("winner"):
        return None
    by_key = {c["key"]: c for c in result["candidates"]}
    winner = by_key[result["winner_key"]]
    current = by_key.get(result.get("current_key"))
    if current and current["key"] == winner["key"]:
        return None
    current_score = (current or {}).get("score") or 0
    winner_score = winner.get("score") or 0
    threshold = 1 + float(tune_cfg.get("improve_pct") or 25) / 100
    if current_score <= 0:
        reason = "текущий профиль больше не проходит"
    elif winner_score >= current_score * threshold:
        reason = f"оценка выше на {round((winner_score / current_score - 1) * 100)}%"
    else:
        return None
    return {"key": winner["key"], "cand": winner["cand"], "label": winner["label"],
            "reason": reason, "score": winner_score, "current_score": current_score,
            "mbps": winner.get("mbps"), "latency_ms": winner.get("latency_ms"),
            "current_mbps": (current or {}).get("mbps"),
            "current_latency_ms": (current or {}).get("latency_ms"),
            "ts": result["ts"]}


def _settle(node, session, cand, report):
    """Сделать кандидата боевым: конфиг на ноде, сервер в списке роутера."""
    spec = {k: cand[k] for k in ("transport", "port", "sni") if k in cand}
    old = node.get("profile")
    new_port = (nb.network_of(cand), int(cand["port"]))
    node_remote.open_ports(session, [new_port])
    report("Записываю боевой конфиг на ноду и перезапускаю ядро", None)
    node_remote.install_production(session, node, spec)
    if old:
        old_port = (nb.network_of(old), int(old["port"]))
        if old_port != new_port:
            node_remote.close_ports(session, [old_port])

    outbound, extras = nb.client_outbounds(node, cand)
    label = nb.describe(cand)
    flags = {}

    def mutate(n, cfg):
        n["profile"] = cand
        n["recommendation"] = None
        server = next((s for s in cfg["servers"] if s["id"] == n.get("server_id")), None)
        if server is None:
            server = {"id": state.new_id()}
            cfg["servers"].append(server)
            n["server_id"] = server["id"]
        server.update({"name": f"{n['name']} · {nb.TRANSPORTS[cand['transport']]['label']}",
                       "url": "", "outbound": outbound, "extra_outbounds": extras,
                       "node_id": n["id"]})
        server.pop("last_test", None)
        if n.pop("activate", False) or not cfg.get("active_server"):
            cfg["active_server"] = server["id"]
            cfg["vpn_enabled"] = True
        flags["active"] = cfg.get("active_server") == server["id"]

    update(node["id"], mutate)
    node_monitor.monitor.restart(node["id"])
    if flags.get("active"):
        report("Перечитываю конфиг роутера — на пару секунд клиенты пойдут напрямую", None)
        result = apply.reconcile()
        if not result.get("ok"):
            report("Применение с замечаниями: " + "; ".join(result.get("errors") or []), None)
    report(f"Боевой профиль: {label}", None)


def _apply(node_id, report, key=None):
    node = get(node_id)
    pool = {c["key"]: c for c in (node.get("last_tune") or {}).get("candidates") or []}
    rec = node.get("recommendation")
    if key is None and rec:
        key = rec["key"]
    if key not in pool:
        raise RuntimeError("такого варианта нет в последнем подборе — запустите подбор заново")
    with _session(node, report) as session:
        _settle(node, session, pool[key]["cand"], report)
    report("Готово", 1.0)


def _restart(node_id, report):
    node = get(node_id)
    with _session(node, report) as session:
        node_remote.restart(session)
    report("Ядро на ноде перезапущено", 1.0)


def _uninstall(node_id, report, wipe=True):
    node = get(node_id)
    if wipe and ssh.key_path(node_id).exists():
        try:
            with _session(node, report) as session:
                report("Удаляю ядро, unit и настройки с сервера", 0.3)
                node_remote.uninstall(session)
                session.remove_pubkey(ssh.ensure_keypair(node_id))
        except Exception as exc:                       # noqa: BLE001
            report(f"С сервера удалить не вышло ({exc}), убираю только из роутера", None)
    remove(node_id)
    report("Нода удалена", 1.0)


def remove(node_id):
    flags = {}

    def mutate(cfg):
        node = find(cfg, node_id)
        if not node:
            return
        cfg["nodes"] = [n for n in cfg["nodes"] if n["id"] != node_id]
        server_id = node.get("server_id")
        if server_id:
            cfg["servers"] = [s for s in cfg["servers"] if s["id"] != server_id]
            if cfg.get("active_server") == server_id:
                cfg["active_server"] = cfg["servers"][0]["id"] if cfg["servers"] else None
                flags["reapply"] = True

    state.update(mutate)
    ssh.forget_keypair(node_id)
    if flags.get("reapply"):
        apply.reconcile()


JOBS = {"deploy": _deploy, "tune": _tune, "apply": _apply,
        "restart": _restart, "uninstall": _uninstall}
TITLES = {"deploy": "развёртывание", "tune": "подбор протокола", "apply": "смена профиля",
          "restart": "перезапуск ядра", "uninstall": "удаление ноды"}


# ── советник ─────────────────────────────────────────────────────────────────

class Advisor:
    """Сам решает, когда перепроверить ноду: по расписанию или потому что
    туннель за последний час несколько раз отваливался."""

    INTERVAL = 60
    DEGRADED_FAILOVERS = 3
    MIN_GAP = 3600

    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="advisor", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        # Не стартуем подбор сразу после запуска контроллера: сеть ещё
        # поднимается, а замеры в этот момент врут.
        if self.stop_event.wait(300):
            return
        while True:
            try:
                self._check()
            except Exception:                          # noqa: BLE001
                log.exception("сбой в советнике")
            if self.stop_event.wait(self.INTERVAL):
                return

    def _check(self):
        if any_running():
            return
        cfg = state.load()
        now = time.time()
        for node in cfg.get("nodes") or []:
            tune_cfg = {**DEFAULT_TUNE, **(node.get("tune") or {})}
            if node.get("status") != "ready" or not node.get("profile") or not tune_cfg["auto"]:
                continue
            last = (node.get("last_tune") or {}).get("ts") or 0
            reason = None
            if now - last > float(tune_cfg["interval_hours"]) * 3600:
                reason = "плановая перепроверка"
            elif (now - last > self.MIN_GAP and node.get("server_id")
                  and tunnel.monitor.failovers_since(now - 3600, node["server_id"])
                  >= self.DEGRADED_FAILOVERS):
                reason = "туннель часто отваливается"
            elif (now - last > self.MIN_GAP and tune_cfg["auto_apply"]
                  and (node.get("recommendation") or {}).get("seen", 2) < 2):
                reason = "подтверждение предложения"
            if reason:
                log.info("советник: перепроверяю ноду %s (%s)", node.get("name"), reason)
                try:
                    start(node["id"], "tune", reason=reason)
                except JobBusy:
                    pass
                return


advisor = Advisor()

"""REST API и раздача веб-панели circle-router."""

import logging
import os
import socket
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (adblock, apply, auth, db, dnslog, dnsmasq_cfg, dockerctl,
               firewall, geodat, netctl, node_monitor, nodes, pppoe, proxylists,
               settings, speedtest, state, stats, subscription, sysinfo, tunnel,
               wanspeed, xray_build, xray_url)

logging.basicConfig(
    level=os.environ.get("CR_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("circle.api")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="circle-router", docs_url=None, redoc_url=None)

# Ключи конфига, которые панель имеет право менять.
EDITABLE = {"vpn_enabled", "bypass", "force_proxy", "wan", "lan", "dhcp", "dns",
            "tproxy", "firewall", "uplink_access", "port_forward", "adblock",
            "speedtest", "block_quic", "log_level", "failover"}


# ── схемы запросов ───────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class ServerPayload(BaseModel):
    name: str = Field(default="Без имени", max_length=80)
    url: str = ""
    outbound: dict | None = None


class ImportRequest(BaseModel):
    url: str
    user_agent: str | None = None
    replace: bool = False


class ParseRequest(BaseModel):
    url: str = ""
    outbound: dict | None = None


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class SpeedTestRequest(BaseModel):
    server_ids: list[str] | None = None


class WanSpeedRequest(BaseModel):
    paths: list[str] | None = None
    duration: int = Field(default=10, ge=5, le=30)


class NodeCreate(BaseModel):
    name: str = Field(default="", max_length=60)
    host: str = Field(max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", max_length=64)
    password: str | None = None
    private_key: str | None = None
    key_passphrase: str | None = None
    sni: list[str] = []
    activate: bool = True
    tune: dict | None = None


class NodeUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=60)
    sni: list[str] | None = None
    tune: dict | None = None


class NodeCredentials(BaseModel):
    password: str | None = None
    private_key: str | None = None
    key_passphrase: str | None = None


class NodeApplyRequest(BaseModel):
    key: str | None = None


# ── вспомогательное ──────────────────────────────────────────────────────────

def _merge(target, patch):
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value
    return target


def _service_status():
    result = {}
    for label, container in (("xray", settings.XRAY_CONTAINER),
                             ("dnsmasq", settings.DNSMASQ_CONTAINER)):
        result[label] = dockerctl.inspect(container)
    return result


def _wan_snapshot(cfg):
    iface = state.wan_interface(cfg)
    gateway = netctl.default_gateway()
    return {
        "mode": cfg["wan"].get("mode", "dhcp"),
        "interface": iface,
        "physical": cfg["wan"]["interface"],
        "link": netctl.link_state(cfg["wan"]["interface"]),
        "carrier": netctl.has_carrier(cfg["wan"]["interface"]),
        "address": netctl.iface_ip(iface),
        "gateway": gateway.get("gateway", ""),
        "gateway_dev": gateway.get("dev", ""),
    }


# ── авторизация ──────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
def login(payload: LoginRequest, response: Response):
    if not auth.verify_credentials(payload.username, payload.password):
        # Небольшая задержка, чтобы перебор по сети был неприятнее.
        time.sleep(0.7)
        raise HTTPException(status_code=401, detail="неверный логин или пароль")
    auth.issue_session(response, payload.username)
    return {"ok": True, "user": payload.username}


@app.post("/api/auth/logout")
def logout(response: Response):
    auth.clear_session(response)
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    user = auth.current_user(request)
    return {"user": user, "authenticated": bool(user), **auth.info()}


@app.post("/api/auth/password")
def change_password(payload: PasswordChangeRequest, response: Response,
                    user: str = Depends(auth.require_user)):
    try:
        auth.change_password(payload.current_password, payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Смена пароля обнуляет все сессии, поэтому текущему браузеру сразу выдаём
    # новую cookie — иначе админ разлогинил бы сам себя.
    auth.issue_session(response, user)
    return {"ok": True}


# ── статус ───────────────────────────────────────────────────────────────────

@app.get("/api/status")
def status(_: str = Depends(auth.require_user)):
    cfg = state.load()
    server = state.active_server(cfg)
    tproxy = cfg["tproxy"]
    effective = xray_build.effective()
    return {
        "vpn": {
            "enabled": bool(cfg.get("vpn_enabled")),
            # Что реально применено: при непринятом сервере xray откатывается
            # на прямой роутинг, хотя галка в конфиге стоит.
            "effective": effective["vpn"],
            "config_error": effective["error"],
            "routing_active": firewall.tproxy_active(),
            "xray_listening": apply.xray_listening(int(tproxy["port"])),
            "watchdog_state": apply.watchdog.state,
            "failover_enabled": bool((cfg.get("failover") or {}).get("enabled", True)),
            "tunnel": tunnel.monitor.snapshot(),
            "active_server": {"id": server.get("id"), "name": server.get("name")}
            if server else None,
        },
        "wan": _wan_snapshot(cfg),
        "lan": {
            "interface": cfg["lan"]["interface"],
            "address": cfg["lan"]["address"],
            "prefix": cfg["lan"]["prefix"],
            "network": str(state.lan_network(cfg)),
            "link": netctl.link_state(cfg["lan"]["interface"]),
            "carrier": netctl.has_carrier(cfg["lan"]["interface"]),
            "addresses": netctl.iface_addresses(cfg["lan"]["interface"]),
        },
        "dhcp": {"enabled": cfg["dhcp"].get("enabled", True),
                 "leases": len(dnsmasq_cfg.leases())},
        "adblock": {"enabled": bool((cfg.get("adblock") or {}).get("enabled", True)),
                    **{k: v for k, v in adblock.status().items()
                       if k in ("domains", "last_update", "updating")},
                    "dns": dnslog.reader.snapshot()},
        # networks идёт ПОСЛЕ спреда: в конфиге лежит поле с тем же именем
        # (пользовательский список), а наружу нужен эффективный — с учётом
        # автоопределения по адресу WAN.
        "uplink": {**(cfg.get("uplink_access") or {}),
                   "networks": firewall.uplink_networks(cfg)},
        "services": _service_status(),
        "pppoe": pppoe.status() if cfg["wan"].get("mode") == "pppoe" else None,
        "host": {
            "uptime": netctl.host_uptime(),
            "loadavg": netctl.loadavg(),
            "time": int(time.time()),
        },
        "last_apply": apply.last_result(),
    }


@app.get("/api/system")
def api_system(_: str = Depends(auth.require_user)):
    """Состояние железа. Данные копит сэмплер, здесь только отдаём готовое."""
    snapshot = dict(stats.sampler.system)
    snapshot["uptime"] = netctl.host_uptime()
    snapshot["info"] = sysinfo.static_info()
    return snapshot


@app.get("/api/interfaces")
def api_interfaces(_: str = Depends(auth.require_user)):
    return netctl.interfaces()


# ── конфигурация ─────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config(_: str = Depends(auth.require_user)):
    cfg = state.load()
    # Пароль PPPoE наружу не отдаём — только признак, что он задан.
    safe = dict(cfg)
    safe["wan"] = dict(cfg["wan"])
    safe["wan"]["pppoe"] = dict(cfg["wan"]["pppoe"])
    safe["wan"]["pppoe"]["password"] = "••••••" if cfg["wan"]["pppoe"].get("password") else ""
    # Ноды живут в своём API: в них ключи Reality и сертификаты.
    safe.pop("nodes", None)
    return safe


@app.post("/api/config")
def post_config(patch: dict, _: str = Depends(auth.require_user)):
    unknown = set(patch) - EDITABLE
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"нельзя менять поля: {', '.join(sorted(unknown))}")

    def mutate(cfg):
        pppoe_patch = ((patch.get("wan") or {}).get("pppoe") or {})
        # Маскированный пароль из GET /api/config не должен затирать настоящий.
        if not str(pppoe_patch.get("password") or "").strip("•"):
            pppoe_patch.pop("password", None)
        _merge(cfg, patch)

    cfg = state.update(mutate)
    problems = dnsmasq_cfg.validate(cfg) + firewall.validate_forwards(cfg)
    return {"ok": not problems, "problems": problems, "config": get_config()}


@app.post("/api/apply")
def post_apply(_: str = Depends(auth.require_user)):
    result = apply.reconcile()
    if not result.get("ok"):
        return JSONResponse(status_code=422, content=result)
    return result


# ── серверы VLESS ────────────────────────────────────────────────────────────

@app.get("/api/servers")
def list_servers(_: str = Depends(auth.require_user)):
    cfg = state.load()
    result = []
    for server in cfg["servers"]:
        item = dict(server)
        try:
            outbound = xray_build.outbound_for(server)
            item["summary"] = xray_url.summarize(outbound)
            item["valid"] = True
        except Exception as exc:                       # noqa: BLE001
            item["summary"] = {}
            item["valid"] = False
            item["error"] = str(exc)
        result.append(item)
    return {"servers": result, "active_server": cfg.get("active_server")}


@app.post("/api/servers")
def add_server(payload: ServerPayload, _: str = Depends(auth.require_user)):
    if not payload.url.strip() and not payload.outbound:
        raise HTTPException(status_code=400, detail="нужна ссылка или outbound")
    if payload.url.strip():
        try:
            xray_url.parse_url(payload.url)
        except Exception as exc:                       # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"ссылка не разобралась: {exc}")

    server = {"id": state.new_id(), "name": payload.name.strip() or "Без имени",
              "url": payload.url.strip()}
    if payload.outbound:
        server["outbound"] = payload.outbound

    def mutate(cfg):
        cfg["servers"].append(server)
        if not cfg.get("active_server"):
            cfg["active_server"] = server["id"]

    state.update(mutate)
    return {"ok": True, "server": server}


@app.put("/api/servers/{server_id}")
def edit_server(server_id: str, payload: ServerPayload,
                _: str = Depends(auth.require_user)):
    if payload.url.strip():
        try:
            xray_url.parse_url(payload.url)
        except Exception as exc:                       # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"ссылка не разобралась: {exc}")

    found = False

    def mutate(cfg):
        nonlocal found
        for server in cfg["servers"]:
            if server["id"] == server_id:
                found = True
                server["name"] = payload.name.strip() or server.get("name", "Без имени")
                server["url"] = payload.url.strip()
                if payload.outbound is not None:
                    server["outbound"] = payload.outbound
                elif server["url"]:
                    server.pop("outbound", None)

    state.update(mutate)
    if not found:
        raise HTTPException(status_code=404, detail="сервер не найден")
    return {"ok": True}


@app.delete("/api/servers/{server_id}")
def delete_server(server_id: str, _: str = Depends(auth.require_user)):
    def mutate(cfg):
        cfg["servers"] = [s for s in cfg["servers"] if s["id"] != server_id]
        if cfg.get("active_server") == server_id:
            cfg["active_server"] = cfg["servers"][0]["id"] if cfg["servers"] else None

    state.update(mutate)
    return {"ok": True}


@app.post("/api/servers/{server_id}/activate")
def activate_server(server_id: str, _: str = Depends(auth.require_user)):
    cfg = state.load()
    if not any(s["id"] == server_id for s in cfg["servers"]):
        raise HTTPException(status_code=404, detail="сервер не найден")
    state.update(lambda c: c.update({"active_server": server_id}))
    return {"ok": True}


@app.post("/api/servers/{server_id}/test")
def test_server(server_id: str, _: str = Depends(auth.require_user)):
    cfg = state.load()
    server = next((s for s in cfg["servers"] if s["id"] == server_id), None)
    if not server:
        raise HTTPException(status_code=404, detail="сервер не найден")
    try:
        outbound = xray_build.outbound_for(server)
        host, port = xray_url.endpoint(outbound)
    except Exception as exc:                           # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"не разобрал сервер: {exc}")
    if outbound.get("protocol") == "hysteria":
        return {"ok": False, "error": "QUIC/UDP: TCP-проба неприменима, используйте замер скорости",
                "host": host, "port": port}

    started = time.perf_counter()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect((host, port))
        latency = (time.perf_counter() - started) * 1000
        return {"ok": True, "latency_ms": round(latency, 1), "host": host, "port": port}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "host": host, "port": port}
    finally:
        sock.close()


@app.post("/api/servers/parse")
def parse_server(payload: ParseRequest, _: str = Depends(auth.require_user)):
    try:
        if payload.url.strip():
            outbound = xray_url.parse_url(payload.url)
        elif payload.outbound:
            outbound = xray_url.normalize_outbound(payload.outbound)
        else:
            raise ValueError("пусто")
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "outbound": outbound, "summary": xray_url.summarize(outbound)}


@app.post("/api/servers/import")
def import_servers(payload: ImportRequest, _: str = Depends(auth.require_user)):
    if not payload.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="ожидается http(s)-ссылка")
    try:
        servers = subscription.import_subscription(
            payload.url, user_agent=payload.user_agent or subscription.DEFAULT_UA)
    except Exception as exc:                           # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc))

    def mutate(cfg):
        if payload.replace:
            cfg["servers"] = list(servers)
        else:
            existing = {s.get("url") for s in cfg["servers"] if s.get("url")}
            cfg["servers"].extend(s for s in servers
                                  if not s.get("url") or s["url"] not in existing)
        if cfg.get("active_server") not in {s["id"] for s in cfg["servers"]}:
            cfg["active_server"] = cfg["servers"][0]["id"] if cfg["servers"] else None

    cfg = state.update(mutate)
    return {"ok": True, "imported": len(servers), "total": len(cfg["servers"])}


# ── свои ноды ────────────────────────────────────────────────────────────────

def _node_or_404(node_id):
    try:
        return nodes.get(node_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="нода не найдена")


def _start_job(node_id, kind, **kwargs):
    try:
        return nodes.start(node_id, kind, **kwargs)
    except nodes.JobBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc))


def _clean_sni(values):
    result = []
    for value in values or []:
        value = value.strip().lower()
        if not value:
            continue
        if any(ch in value for ch in " /:") or "." not in value:
            raise HTTPException(status_code=400, detail=f"не похоже на домен: {value}")
        if "apple" in value or "icloud" in value:
            raise HTTPException(status_code=400,
                                detail="apple/icloud в Reality ядро не любит, такие SNI банят первыми")
        if value not in result:
            result.append(value)
    return result


@app.get("/api/nodes")
def list_nodes(_: str = Depends(auth.require_user)):
    cfg = state.load()
    result = []
    for node in cfg.get("nodes") or []:
        item = nodes.public(node, cfg)
        item["metrics"], _history = node_monitor.monitor.snapshot(node["id"])
        result.append(item)
    return {"nodes": result, "busy": nodes.any_running()}


@app.post("/api/nodes")
def create_node(payload: NodeCreate, _: str = Depends(auth.require_user)):
    host = payload.host.strip()
    if not host or any(ch in host for ch in " /@"):
        raise HTTPException(status_code=400, detail="укажите IP или имя сервера")
    if not (payload.password or payload.private_key):
        raise HTTPException(status_code=400, detail="нужен пароль или приватный ключ")
    if nodes.any_running():
        raise HTTPException(status_code=409, detail="дождитесь окончания текущей задачи")
    node = nodes.create({
        "name": payload.name.strip() or host, "host": host,
        "ssh_port": payload.ssh_port, "ssh_user": payload.ssh_user.strip() or "root",
        "sni": _clean_sni(payload.sni), "activate": payload.activate,
        "tune": {k: v for k, v in (payload.tune or {}).items() if k in nodes.DEFAULT_TUNE},
    })
    _start_job(node["id"], "deploy", password=payload.password,
               private_key=payload.private_key, key_passphrase=payload.key_passphrase)
    return {"ok": True, "node": nodes.public(node, state.load())}


@app.put("/api/nodes/{node_id}")
def edit_node(node_id: str, payload: NodeUpdate, _: str = Depends(auth.require_user)):
    _node_or_404(node_id)
    sni = _clean_sni(payload.sni) if payload.sni is not None else None

    def mutate(node, cfg):
        if payload.name is not None and payload.name.strip():
            node["name"] = payload.name.strip()
            server = next((s for s in cfg["servers"] if s["id"] == node.get("server_id")), None)
            if server and node.get("profile"):
                label = nodes.nb.TRANSPORTS[node["profile"]["transport"]]["label"]
                server["name"] = f"{node['name']} · {label}"
        if sni is not None:
            node["sni"] = sni
        if payload.tune:
            tune = {**nodes.DEFAULT_TUNE, **(node.get("tune") or {})}
            for key, value in payload.tune.items():
                if key in nodes.DEFAULT_TUNE:
                    tune[key] = value
            tune["interval_hours"] = min(max(float(tune["interval_hours"]), 1), 168)
            tune["speed_seconds"] = min(max(int(tune["speed_seconds"]), 3), 15)
            tune["improve_pct"] = min(max(float(tune["improve_pct"]), 5), 200)
            node["tune"] = tune

    node = nodes.update(node_id, mutate)
    return {"ok": True, "node": nodes.public(node, state.load())}


@app.post("/api/nodes/{node_id}/deploy")
def redeploy_node(node_id: str, payload: NodeCredentials,
                  _: str = Depends(auth.require_user)):
    _node_or_404(node_id)
    return {"ok": True, "job": _start_job(node_id, "deploy", password=payload.password,
                                          private_key=payload.private_key,
                                          key_passphrase=payload.key_passphrase)}


@app.post("/api/nodes/{node_id}/tune")
def tune_node(node_id: str, _: str = Depends(auth.require_user)):
    node = _node_or_404(node_id)
    if node.get("status") != "ready":
        raise HTTPException(status_code=400, detail="нода ещё не развёрнута")
    return {"ok": True, "job": _start_job(node_id, "tune")}


@app.post("/api/nodes/{node_id}/apply")
def apply_node_profile(node_id: str, payload: NodeApplyRequest,
                       _: str = Depends(auth.require_user)):
    _node_or_404(node_id)
    return {"ok": True, "job": _start_job(node_id, "apply", key=payload.key)}


@app.post("/api/nodes/{node_id}/restart")
def restart_node(node_id: str, _: str = Depends(auth.require_user)):
    _node_or_404(node_id)
    return {"ok": True, "job": _start_job(node_id, "restart")}


@app.post("/api/nodes/{node_id}/forget-host-key")
def forget_host_key(node_id: str, _: str = Depends(auth.require_user)):
    _node_or_404(node_id)
    nodes.update(node_id, lambda n, _: n.update({"host_key": None}))
    return {"ok": True}


@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: str, wipe: bool = True, _: str = Depends(auth.require_user)):
    _node_or_404(node_id)
    return {"ok": True, "job": _start_job(node_id, "uninstall", wipe=wipe)}


@app.get("/api/nodes/{node_id}/metrics")
def node_metrics(node_id: str, _: str = Depends(auth.require_user)):
    node = _node_or_404(node_id)
    cfg = state.load()
    latest, history = node_monitor.monitor.snapshot(node_id)
    active = bool(node.get("server_id")) and cfg.get("active_server") == node["server_id"]
    link = stats.sampler.snapshot(150) if active else []
    return {
        "latest": latest,
        "history": history,
        "active": active,
        "tunnel": tunnel.monitor.snapshot() if active else None,
        "tunnel_series": tunnel.monitor.series() if active else [],
        "throughput": [{"ts": p["ts"], "down": p.get("proxy_down_rate", 0),
                        "up": p.get("proxy_up_rate", 0)} for p in link
                       if "proxy_down_rate" in p],
    }


# ── клиенты и метрики ────────────────────────────────────────────────────────

@app.get("/api/clients")
def clients(_: str = Depends(auth.require_user)):
    leases = dnsmasq_cfg.leases()
    rates = stats.sampler.client_rates
    day_totals = db.client_totals(86400)
    by_ip = {lease["ip"]: lease for lease in leases}
    for ip in set(rates) | set(day_totals):
        by_ip.setdefault(ip, {"ip": ip, "mac": "", "hostname": "", "expires_in": 0})
    result = []
    for ip, lease in by_ip.items():
        rate = rates.get(ip, {})
        result.append({
            **lease,
            "up_rate": rate.get("up_rate", 0.0),
            "down_rate": rate.get("down_rate", 0.0),
            "up_total": rate.get("up_total", 0),
            "down_total": rate.get("down_total", 0),
            "up_day": day_totals.get(ip, {}).get("up", 0),
            "down_day": day_totals.get(ip, {}).get("down", 0),
            "online": bool(rate.get("up_rate") or rate.get("down_rate")),
        })
    result.sort(key=lambda c: c["down_rate"] + c["up_rate"], reverse=True)
    return result


@app.get("/api/metrics/live")
def metrics_live(points: int = 120, _: str = Depends(auth.require_user)):
    return {"samples": stats.sampler.snapshot(min(max(points, 10), 300)),
            "current": stats.sampler.current_rates()}


RANGES = {"1h": (3600, 60), "6h": (21600, 300), "24h": (86400, 900),
          "7d": (604800, 3600), "30d": (2592000, 21600)}


@app.get("/api/metrics/history")
def metrics_history(range: str = "24h", _: str = Depends(auth.require_user)):
    if range not in RANGES:
        raise HTTPException(status_code=400,
                            detail=f"диапазон должен быть одним из {', '.join(RANGES)}")
    seconds, bucket = RANGES[range]
    return {"range": range, "bucket": bucket,
            "points": db.history(seconds, bucket), "totals": db.totals(seconds)}


# ── блокировщик рекламы ──────────────────────────────────────────────────────

@app.get("/api/adblock")
def adblock_status(_: str = Depends(auth.require_user)):
    cfg = state.load()
    return {"config": cfg.get("adblock"), "status": adblock.status(),
            "dns": dnslog.reader.snapshot()}


@app.post("/api/adblock/update")
def adblock_update(_: str = Depends(auth.require_user)):
    cfg = state.load()
    try:
        changed, report = adblock.update(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:                           # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    if changed:
        dockerctl.restart(settings.DNSMASQ_CONTAINER)
    return {"ok": True, "restarted": changed, **report}


def _proxylists_changed(changed):
    """Новые списки: статические подсети — в nft, домены — в dnsmasq."""
    cfg = state.load()
    if firewall.tproxy_active():
        firewall.apply_tproxy(cfg, True)
    if changed:
        dockerctl.restart(settings.DNSMASQ_CONTAINER)


@app.get("/api/smart-proxy")
def smart_proxy_status(_: str = Depends(auth.require_user)):
    cfg = state.load()
    return {"config": cfg.get("smart_proxy"), "enabled": proxylists.enabled(cfg),
            "status": proxylists.status()}


@app.post("/api/smart-proxy/update")
def smart_proxy_update(_: str = Depends(auth.require_user)):
    cfg = state.load()
    try:
        changed, report = proxylists.update(cfg)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:                           # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    _proxylists_changed(changed)
    return {"ok": True, "restarted": changed, **report}


@app.post("/api/adblock/reset-stats")
def adblock_reset_stats(_: str = Depends(auth.require_user)):
    dnslog.reader.reset()
    return {"ok": True}


# ── замер скорости серверов ──────────────────────────────────────────────────

@app.get("/api/speedtest")
def speedtest_status(_: str = Depends(auth.require_user)):
    return speedtest.status()


@app.post("/api/speedtest")
def speedtest_start(payload: SpeedTestRequest, _: str = Depends(auth.require_user)):
    cfg = state.load()
    known = [s["id"] for s in cfg.get("servers") or []]
    requested = payload.server_ids or known
    targets = [sid for sid in requested if sid in known]
    if not targets:
        raise HTTPException(status_code=400, detail="нечего проверять")
    try:
        return speedtest.start(targets)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ── общий спидтест канала ────────────────────────────────────────────────────

@app.get("/api/wanspeed")
def wanspeed_status(_: str = Depends(auth.require_user)):
    return wanspeed.status()


@app.post("/api/wanspeed")
def wanspeed_start(payload: WanSpeedRequest, _: str = Depends(auth.require_user)):
    try:
        return wanspeed.start(payload.paths, payload.duration)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# ── geo-базы, логи, сервисы ──────────────────────────────────────────────────

@app.get("/api/geo/categories")
def geo_categories(_: str = Depends(auth.require_user)):
    return geodat.categories()


@app.post("/api/geo/update")
def geo_update(_: str = Depends(auth.require_user)):
    try:
        result = geodat.update()
    except Exception as exc:                           # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    dockerctl.restart(settings.XRAY_CONTAINER)
    return {"ok": True, **result}


SERVICES = {"xray": settings.XRAY_CONTAINER, "dnsmasq": settings.DNSMASQ_CONTAINER}


@app.get("/api/logs/{service}")
def service_logs(service: str, tail: int = 200, _: str = Depends(auth.require_user)):
    if service not in SERVICES:
        raise HTTPException(status_code=404, detail="нет такого сервиса")
    return {"service": service,
            "log": dockerctl.logs(SERVICES[service], tail=min(max(tail, 10), 2000))}


@app.post("/api/services/{service}/restart")
def restart_service(service: str, _: str = Depends(auth.require_user)):
    if service not in SERVICES:
        raise HTTPException(status_code=404, detail="нет такого сервиса")
    ok, err = dockerctl.restart(SERVICES[service])
    if not ok:
        raise HTTPException(status_code=502, detail=err)
    return {"ok": True}


@app.post("/api/pppoe/restart")
def restart_pppoe(_: str = Depends(auth.require_user)):
    cfg = state.load()
    if cfg["wan"].get("mode") != "pppoe":
        raise HTTPException(status_code=400, detail="WAN не в режиме PPPoE")
    pppoe.restart(cfg)
    return {"ok": True}


# ── жизненный цикл и статика ─────────────────────────────────────────────────

@app.on_event("startup")
def on_startup():
    db.init()
    log.info("стартую controller, данные в %s", settings.DATA)
    try:
        apply.reconcile(restart_services=False)
    except Exception:                                  # noqa: BLE001
        log.exception("первичное применение конфигурации не удалось")
    stats.sampler.start()
    tunnel.monitor.start()
    apply.watchdog.start()
    node_monitor.monitor.start()
    nodes.advisor.start()
    dnslog.reader.start()
    adblock.updater.start(on_change=lambda: dockerctl.restart(settings.DNSMASQ_CONTAINER))
    proxylists.updater.start(on_change=_proxylists_changed)


@app.on_event("shutdown")
def on_shutdown():
    apply.watchdog.stop()
    tunnel.monitor.stop()
    node_monitor.monitor.stop()
    nodes.advisor.stop()
    stats.sampler.stop()
    dnslog.reader.stop()
    adblock.updater.stop()
    proxylists.updater.stop()


@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": int(time.time())}


if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")

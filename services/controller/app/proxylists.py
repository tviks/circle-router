"""Умный прокси: в VPN уходит только то, что без него не открывается.

Решение принимается ещё на DNS. Для доменов из списков dnsmasq спрашивает DNS
через xray (ответ не подменит провайдер, а CDN отдаст адреса рядом с выходом
VPN) и складывает полученные IP в nft-сет `proxy_dst`. TPROXY заворачивает в
xray только соединения к адресам из этого сета и к статическим подсетям
(Telegram, Meta и т.п. — у них есть голые IP без DNS). Всё остальное ядро
маршрутизирует само, с flowtable, не копируя трафик через userspace.

Скачанные списки кешируются на диск по той же схеме, что и у adblock:
«Применить» пересобирает файл из кеша без похода в сеть.
"""

import hashlib
import ipaddress
import json
import logging
import os
import re
import threading
import time

import httpx

from . import settings, xray_build

log = logging.getLogger("circle.proxylists")

CACHE_DIR = settings.CACHE_DIR / "proxylists"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

CONF_PATH = settings.DATA / "dnsmasq" / "proxy.conf"
# Тот же файл глазами контейнера dnsmasq.
CONF_IN_CONTAINER = "/etc/dnsmasq/proxy.conf"
META_PATH = settings.DATA / "config" / "proxylists.json"

# Сет, который наполняет dnsmasq. Живёт в таблице TPROXY: nft не умеет
# ссылаться на сеты чужих таблиц.
DYN_SET = "proxy_dst"
STATIC_SET = "proxy_static"

# Предел длины строки server=/nftset=. Буфер строки конфига у dnsmasq около
# 1 КБ: длиннее — и он падает с «bad option», оставляя LAN без DNS и DHCP.
_MAX_LINE = 900

# Домен или голый TLD (в списке itdoginfo есть «.ua» — вся зона целиком).
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)"
                        r"(\.(?!-)[a-z0-9-]{1,63}(?<!-))*$")

_state = {"updating": False, "last_update": 0, "last_build": 0,
          "domains": 0, "subnets": 0, "sources": [], "error": None}
_lock = threading.RLock()
_build_lock = threading.Lock()


def _section(cfg):
    return cfg.get("smart_proxy") or {}


def enabled(cfg):
    """Умный режим действует, только когда VPN вообще включён."""
    return bool(_section(cfg).get("enabled")) and bool(cfg.get("vpn_enabled"))


# ── разбор ──────────────────────────────────────────────────────────────────

def _domain(raw):
    raw = (raw or "").strip().lower()
    # Префиксы в стиле xray из force_proxy: domain:/full: сводятся к домену,
    # regexp:/keyword:/geosite: на уровне DNS не выразить — пропускаем.
    if ":" in raw:
        kind, _, value = raw.partition(":")
        if kind not in ("domain", "full"):
            return None
        raw = value
    raw = raw.strip().strip(".")
    return raw if raw and _DOMAIN_RE.match(raw) else None


def parse_domains(text):
    result = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        domain = _domain(line)
        if domain:
            result.add(domain)
    return result


def parse_subnets(text):
    result = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        try:
            net = ipaddress.ip_network(line, strict=False)
        except ValueError:
            continue
        if net.version == 4:
            result.append(net)
    return result


def _drop_covered(domains):
    """Поддомен лишний, если в наборе уже есть родитель: dnsmasq матчит суффикс."""
    kept = []
    for domain in domains:
        parts = domain.split(".")
        if any(".".join(parts[i:]) in domains for i in range(1, len(parts))):
            continue
        kept.append(domain)
    return sorted(kept)


# ── кеш источников ──────────────────────────────────────────────────────────

def _cache_path(url):
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".txt")


def _source_text(url, download):
    path = _cache_path(url)
    if download:
        with httpx.Client(follow_redirects=True, timeout=120,
                          headers={"User-Agent": "circle-router/1.0"}) as client:
            resp = client.get(url)
            resp.raise_for_status()
            text = resp.text
        path.write_text(text, encoding="utf-8")
        return text
    if path.exists():
        return path.read_text(encoding="utf-8")
    raise FileNotFoundError("список ещё не скачан, обновление идёт в фоне")


def _collect(cfg, key, parser, download, report):
    items = []
    for source in _section(cfg).get(key) or []:
        if not source.get("enabled", True) or not source.get("url"):
            continue
        entry = {"name": source.get("name") or source["url"], "ok": True}
        try:
            parsed = parser(_source_text(source["url"], download))
            items.append(parsed)
            entry["count"] = len(parsed)
        except Exception as exc:                       # noqa: BLE001
            entry.update({"ok": False, "error": str(exc)})
        report["sources"].append(entry)
    return items


# ── итоговые наборы ─────────────────────────────────────────────────────────

def domains(cfg, download=False, report=None):
    report = report if report is not None else {"sources": []}
    result = set()
    for parsed in _collect(cfg, "domain_lists", parse_domains, download, report):
        result |= parsed
    for raw in (cfg.get("force_proxy") or {}).get("domains") or []:
        domain = _domain(raw)
        if domain:
            result.add(domain)
    # Явный bypass пользователя сильнее чужого списка.
    for raw in (cfg.get("bypass") or {}).get("domains") or []:
        result.discard(_domain(raw))
    return _drop_covered(result)


def subnets(cfg, download=False, report=None):
    """Статические подсети для сета proxy_static, без пересечений."""
    report = report if report is not None else {"sources": []}
    nets = []
    for parsed in _collect(cfg, "subnet_lists", parse_subnets, download, report):
        nets.extend(parsed)
    for raw in (cfg.get("force_proxy") or {}).get("ips") or []:
        try:
            net = ipaddress.ip_network(str(raw).strip(), strict=False)
        except ValueError:
            continue                                   # geoip:* и прочее — забота xray
        if net.version == 4:
            nets.append(net)
    return [str(n) for n in ipaddress.collapse_addresses(nets)]


def cached_subnets(cfg):
    """Для firewall: только из кеша, сеть не трогаем никогда."""
    if not enabled(cfg):
        return []
    try:
        return subnets(cfg, download=False)
    except Exception:                                  # noqa: BLE001
        log.exception("не удалось собрать статические подсети")
        return []


def render(cfg, domain_list):
    lines = ["# Сгенерировано circle-router (умный прокси). Правки руками перезапишутся."]
    if not domain_list:
        return "\n".join(lines) + "\n"
    upstream = f"127.0.0.1#{xray_build.DNS_INBOUND_PORT}"
    nftset = f"4#ip#{settings.NFT_MANGLE}#{DYN_SET}"
    for chunk in _chunks(domain_list, _MAX_LINE - len("nftset=//") - len(nftset)):
        joined = "/".join(chunk)
        # Резолвим через xray (то есть через VPN) и кладём ответы в сет.
        lines.append(f"server=/{joined}/{upstream}")
        lines.append(f"nftset=/{joined}/{nftset}")
    return "\n".join(lines) + "\n"


def _chunks(items, limit):
    """Режет список на группы, чья запись через «/» не длиннее limit."""
    chunk, size = [], 0
    for item in items:
        if chunk and size + 1 + len(item) > limit:
            yield chunk
            chunk, size = [], 0
        chunk.append(item)
        size += len(item) + (1 if size else 0)
    if chunk:
        yield chunk


def _write(body):
    if CONF_PATH.exists() and CONF_PATH.read_text(encoding="utf-8") == body:
        return False
    tmp = CONF_PATH.with_suffix(".conf.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, CONF_PATH)
    return True


def build(cfg, download=False):
    """Пересобрать proxy.conf. Возвращает (changed, report)."""
    with _build_lock:
        report = {"sources": []}
        if enabled(cfg):
            domain_list = domains(cfg, download, report)
            subnet_list = subnets(cfg, download=False)
        else:
            domain_list, subnet_list = [], []
            report["disabled"] = True
        changed = _write(render(cfg, domain_list))
        report.update({"domains": len(domain_list), "subnets": len(subnet_list)})
        _remember(report, download)
        return changed, report


def _remember(report, downloaded):
    now = int(time.time())
    with _lock:
        _state.update({"last_build": now, "domains": report.get("domains", 0),
                       "subnets": report.get("subnets", 0),
                       "sources": report.get("sources", []), "error": None})
        if downloaded:
            _state["last_update"] = now
        snapshot = {k: v for k, v in _state.items() if k != "updating"}
    try:
        META_PATH.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _restore():
    if not META_PATH.exists():
        return
    try:
        stored = json.loads(META_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    with _lock:
        for key in ("last_update", "last_build", "domains", "subnets", "sources"):
            if key in stored:
                _state[key] = stored[key]


def status():
    with _lock:
        return dict(_state)


def update(cfg):
    """Скачать списки заново. Статические подсети тоже качаются здесь."""
    with _lock:
        if _state["updating"]:
            raise RuntimeError("обновление уже идёт")
        _state["updating"] = True
    try:
        if enabled(cfg):
            subnets(cfg, download=True)
        return build(cfg, download=True)
    finally:
        with _lock:
            _state["updating"] = False


class Updater:
    """Фоновое обновление списков по расписанию."""

    CHECK_INTERVAL = 900

    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()
        self.on_change = None

    def start(self, on_change=None):
        if self.thread and self.thread.is_alive():
            return
        self.on_change = on_change
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="proxylists", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        from . import state
        if self.stop_event.wait(15):
            return
        while not self.stop_event.is_set():
            try:
                cfg = state.load()
                if enabled(cfg):
                    hours = int(_section(cfg).get("auto_update_hours") or 24)
                    last = status()["last_update"]
                    if not last or time.time() - last > hours * 3600:
                        changed, report = update(cfg)
                        log.info("списки умного прокси обновлены: %s доменов, %s подсетей",
                                 report.get("domains"), report.get("subnets"))
                        if self.on_change:
                            self.on_change(changed)
            except Exception:                          # noqa: BLE001
                log.exception("не удалось обновить списки умного прокси")
            if self.stop_event.wait(self.CHECK_INTERVAL):
                return


updater = Updater()
_restore()

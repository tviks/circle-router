"""Блокировщик рекламы на уровне DNS.

Списки скачиваются, сводятся в один набор доменов и раскладываются в конфиг
dnsmasq строками `address=/домен/0.0.0.0`. Такая запись накрывает и сам домен,
и все его поддомены, поэтому одна строка заменяет десятки записей hosts.

Скачанные списки кешируются на диск: пересборка после правки своего списка или
allowlist не должна лезть в сеть — это делает «Применить» мгновенным.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time

import httpx

from . import settings

log = logging.getLogger("circle.adblock")

CACHE_DIR = settings.CACHE_DIR / "adblock"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Без этих имён ломается разрешение локальных адресов, а пользы для блокировки
# рекламы никакой.
NEVER_BLOCK = {
    "localhost", "localhost.localdomain", "local", "broadcasthost",
    "ip6-localhost", "ip6-loopback", "ip6-localnet", "ip6-mcastprefix",
    "ip6-allnodes", "ip6-allrouters", "ip6-allhosts",
}

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)"
                        r"(\.(?!-)[a-z0-9-]{1,63}(?<!-))+$")

_state = {
    "updating": False,
    "last_update": 0,
    "last_build": 0,
    "domains": 0,
    "sources": [],
    "error": None,
}
_lock = threading.RLock()
_build_lock = threading.Lock()


# ── разбор списков ───────────────────────────────────────────────────────────

def _valid(domain):
    domain = (domain or "").strip().rstrip(".").lower()
    if not domain or domain in NEVER_BLOCK:
        return None
    return domain if _DOMAIN_RE.match(domain) else None


def parse(text):
    """Понимает hosts, AdGuard/AdBlock, dnsmasq и просто списки доменов."""
    domains = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line[0] in "#!;[":
            continue

        # Исключения AdBlock (@@||domain^) значат «не блокировать» — для этого
        # есть отдельный allowlist в конфиге.
        if line.startswith("@@"):
            continue

        if line.startswith("||"):
            body = line[2:].split("$", 1)[0].split("^", 1)[0]
            if "/" in body or "*" in body:
                continue
            domain = _valid(body)
            if domain:
                domains.add(domain)
            continue

        if line.startswith(("local=/", "address=/", "server=/")):
            parts = line.split("/")
            if len(parts) >= 2:
                domain = _valid(parts[1])
                if domain:
                    domains.add(domain)
            continue

        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2 and fields[0] in ("0.0.0.0", "127.0.0.1", "::", "::1"):
            for field in fields[1:]:
                domain = _valid(field)
                if domain:
                    domains.add(domain)
        elif len(fields) == 1:
            domain = _valid(fields[0])
            if domain:
                domains.add(domain)

    return domains


# ── кеш источников ───────────────────────────────────────────────────────────

def _cache_path(url):
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".txt")


def _download(url, timeout=180):
    with httpx.Client(follow_redirects=True, timeout=timeout,
                      headers={"User-Agent": "circle-router/1.0"}) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text


class NotCached(Exception):
    """Список ещё не скачан, а лезть в сеть в этом вызове нельзя."""


class Stale(Exception):
    """Скачать не вышло, но есть прошлая версия из кеша."""

    def __init__(self, text, cause):
        super().__init__(str(cause))
        self.text = text


def _source_text(url, download):
    """(текст, из_кеша). При download=False сеть не трогаем вовсе.

    Это важно на старте: `reconcile` не должен на первом же запуске висеть
    минуту, качая мегабайты. Пустой список подхватит фоновый updater.
    """
    path = _cache_path(url)
    if download:
        try:
            text = _download(url)
        except Exception as exc:
            # Сеть подвела — живём на прошлой версии. Иначе один сбой DNS
            # обнулял блок-лист целиком до следующего удачного обновления.
            if path.exists():
                raise Stale(path.read_text(encoding="utf-8"), exc) from exc
            raise
        path.write_text(text, encoding="utf-8")
        return text, False
    if path.exists():
        return path.read_text(encoding="utf-8"), True
    raise NotCached("список ещё не скачан, обновление идёт в фоне")


# ── сборка файла для dnsmasq ─────────────────────────────────────────────────

def _covered_by_parent(domain, domains):
    """`address=/example.com/` уже накрывает ads.example.com."""
    parts = domain.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in domains:
            return True
    return False


def render(domains, allowlist):
    # Никаких меток времени в теле файла: содержимое сравнивается побайтово,
    # чтобы понять, нужен ли рестарт dnsmasq. Волатильная строка заставляла бы
    # его перезапускаться на каждое «Применить». Время сборки — в adblock.json.
    lines = [
        "# Сгенерировано circle-router (блокировщик рекламы).",
        f"# Доменов: {len(domains)}",
        "",
    ]
    lines += [f"address=/{d}/0.0.0.0" for d in sorted(domains)]
    if allowlist:
        lines += [
            "",
            "# Исключения. В dnsmasq выигрывает более длинное совпадение,",
            "# поэтому эти домены резолвятся обычным путём даже если их",
            "# родитель заблокирован.",
        ]
        lines += [f"server=/{d}/#" for d in sorted(allowlist)]
    return "\n".join(lines) + "\n"


def build(cfg, download=False):
    """Собирает blocklist.conf. Возвращает (файл изменился, отчёт)."""
    with _build_lock:
        adblock = cfg.get("adblock") or {}
        report = {"sources": [], "domains": 0, "downloaded": download}
        fresh = False

        if not adblock.get("enabled", True):
            changed = _write("# Блокировщик выключен в панели.\n")
            report["disabled"] = True
            _remember(report, download)
            return changed, report

        allowlist = {d for d in map(_valid, adblock.get("allowlist") or []) if d}
        domains = {d for d in map(_valid, adblock.get("blocklist") or []) if d}
        if domains:
            report["sources"].append({"name": "Свой список", "url": "",
                                      "count": len(domains), "ok": True})

        for source in adblock.get("sources") or []:
            if not source.get("enabled", True) or not source.get("url"):
                continue
            entry = {"name": source.get("name") or source["url"], "url": source["url"]}
            try:
                text, cached = _source_text(source["url"], download)
                parsed = parse(text)
                domains |= parsed
                fresh = fresh or not cached
                entry.update({"count": len(parsed), "ok": True, "cached": cached})
            except Stale as exc:
                parsed = parse(exc.text)
                domains |= parsed
                entry.update({"count": len(parsed), "ok": False, "cached": True,
                              "error": f"не обновился ({str(exc)[:120]}), работает прошлая версия"})
                log.warning("список %s не обновился, беру кеш: %s", source["url"], exc)
            except NotCached as exc:
                # Не ошибка конфигурации — просто ещё не успели скачать.
                entry.update({"count": 0, "ok": True, "pending": True, "note": str(exc)})
            except Exception as exc:                   # noqa: BLE001
                entry.update({"count": 0, "ok": False, "error": str(exc)[:200]})
                log.warning("список %s не загрузился: %s", source["url"], exc)
            report["sources"].append(entry)

        domains -= allowlist
        # Поддомены, у которых уже заблокирован родитель, не нужны: файл меньше,
        # dnsmasq стартует быстрее, результат тот же.
        domains = {d for d in domains if not _covered_by_parent(d, domains)}

        report["domains"] = len(domains)
        changed = _write(render(domains, allowlist))
        # Метку «обновлено» ставим только за реально скачанное: иначе после
        # сбоя сети следующая попытка отложилась бы на сутки.
        _remember(report, download and fresh)
        return changed, report


def _write(body):
    path = settings.BLOCKLIST_PATH
    if path.exists() and path.read_text(encoding="utf-8") == body:
        return False
    tmp = path.with_suffix(".conf.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return True


def _remember(report, downloaded):
    now = int(time.time())
    with _lock:
        _state["last_build"] = now
        _state["domains"] = report.get("domains", 0)
        _state["sources"] = report.get("sources", [])
        _state["error"] = report.get("error")
        if downloaded:
            _state["last_update"] = now
        snapshot = {k: v for k, v in _state.items() if k != "updating"}
    try:
        settings.BLOCKLIST_META.write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _restore():
    if not settings.BLOCKLIST_META.exists():
        return
    try:
        stored = json.loads(settings.BLOCKLIST_META.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    with _lock:
        for key in ("last_update", "last_build", "domains", "sources", "error"):
            if key in stored:
                _state[key] = stored[key]


def status():
    with _lock:
        snapshot = dict(_state)
    snapshot["file_size"] = (settings.BLOCKLIST_PATH.stat().st_size
                             if settings.BLOCKLIST_PATH.exists() else 0)
    return snapshot


def update(cfg):
    """Обновление со скачиванием. Возвращает (changed, report)."""
    with _lock:
        if _state["updating"]:
            raise RuntimeError("обновление уже идёт")
        _state["updating"] = True
    try:
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
        self.thread = threading.Thread(target=self._loop, name="adblock", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        from . import state
        # На старте не бежим сразу в сеть: сначала даём подняться WAN.
        if self.stop_event.wait(20):
            return
        while not self.stop_event.is_set():
            try:
                cfg = state.load()
                adblock = cfg.get("adblock") or {}
                if adblock.get("enabled", True):
                    hours = int(adblock.get("auto_update_hours") or 24)
                    last = status()["last_update"]
                    if not last or time.time() - last > hours * 3600:
                        changed, report = update(cfg)
                        log.info("списки блокировки обновлены: %s доменов",
                                 report.get("domains"))
                        if changed and self.on_change:
                            self.on_change()
            except Exception:                          # noqa: BLE001
                log.exception("не удалось обновить списки блокировки")
            if self.stop_event.wait(self.CHECK_INTERVAL):
                return


updater = Updater()
_restore()

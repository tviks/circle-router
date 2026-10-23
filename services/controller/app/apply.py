"""Приведение системы к состоянию, описанному в router.json."""

import logging
import threading
import time

from . import (adblock, dnsmasq_cfg, dockerctl, firewall, netctl, pppoe,
               proxylists, settings, state, tunnel, xray_build)
from .shell import run

log = logging.getLogger("circle.apply")

_apply_lock = threading.Lock()
_last_result = {"ok": None, "ts": 0, "steps": [], "errors": []}


def last_result():
    return dict(_last_result)


def _configure_static_wan(cfg):
    """Ручной WAN: адрес, шлюз. DNS-серверы уходят в конфиг dnsmasq."""
    wan = cfg["wan"]
    iface = wan["interface"]
    address = (wan.get("static") or {}).get("address", "").strip()
    gateway = (wan.get("static") or {}).get("gateway", "").strip()
    if not address:
        return "статический WAN: адрес не задан, пропускаю"
    run(["ip", "addr", "replace", address, "dev", iface], check=False)
    run(["ip", "link", "set", iface, "up"], check=False)
    if gateway:
        run(["ip", "route", "replace", "default", "via", gateway, "dev", iface],
            check=False)
    return f"статический WAN: {address} via {gateway or '—'}"


def reconcile(restart_services=True):
    """Полный проход: сеть -> фаервол -> конфиги сервисов -> рестарты."""
    with _apply_lock:
        cfg = state.load()
        steps, errors = [], []

        # Валидация НЕ прерывает проход. Раньше одно недозаполненное правило
        # проброса роняло весь reconcile — вместе с подъёмом PPPoE, и после
        # перезапуска контроллера дом оставался без интернета из-за опечатки
        # в форме. Негодные правила просто не попадают в ruleset
        # (см. active_forwards), а претензии уходят в отчёт.
        dhcp_problems = dnsmasq_cfg.validate(cfg)
        errors.extend(dhcp_problems)
        errors.extend(firewall.validate_forwards(cfg))

        sysctls = netctl.prepare_host(cfg["lan"]["interface"], cfg["wan"]["interface"])
        failed = [k for k, ok in sysctls.items() if not ok]
        if failed:
            errors.append("не удалось выставить sysctl: " + ", ".join(failed))
        steps.append("sysctl и модули ядра")

        netctl.mark_unmanaged(cfg["lan"]["interface"])
        address = netctl.configure_lan(cfg["lan"]["interface"], cfg["lan"]["address"],
                                       cfg["lan"]["prefix"])
        steps.append(f"LAN {cfg['lan']['interface']} -> {address}")

        mode = cfg["wan"].get("mode", "dhcp")
        if mode == "pppoe":
            pppoe.start(cfg)
            steps.append("PPPoE-сессия запущена")
        else:
            pppoe.stop()
            if mode == "static":
                steps.append(_configure_static_wan(cfg))
            else:
                steps.append("WAN в режиме DHCP (адресом управляет netplan)")

        try:
            firewall.apply_base(cfg)
            steps.append("nftables: фаервол и NAT")
        except Exception as exc:                       # noqa: BLE001
            errors.append(f"nftables (базовые правила): {exc}")

        # Цепочку TPROXY обычно переписывает watchdog, но только на переходах
        # вкл/выкл. Без этого правки bypass-списков, подсетей и проброса
        # висели бы в старой редакции до ближайшего падения xray.
        if firewall.tproxy_active():
            try:
                firewall.apply_tproxy(cfg, True)
                steps.append("nftables: правила TPROXY обновлены")
            except Exception as exc:                   # noqa: BLE001
                errors.append(f"nftables (TPROXY): {exc}")

        try:
            if firewall.apply_docker_user(cfg):
                steps.append("nftables: разрешение транзита в цепочке Docker")
        except Exception as exc:                       # noqa: BLE001
            errors.append(f"nftables (DOCKER-USER): {exc}")

        try:
            if firewall.apply_accounting(cfg):
                steps.append("nftables: учёт трафика по клиентам")
        except Exception as exc:                       # noqa: BLE001
            errors.append(f"nftables (учёт трафика): {exc}")

        vpn_enabled, xray_error = xray_build.write(cfg)
        if xray_error:
            errors.append(f"конфиг xray: {xray_error}")
        steps.append(f"конфиг xray записан (vpn={'on' if vpn_enabled else 'off'})")

        # Пересобираем список блокировки из кеша — без похода в сеть, иначе
        # каждое «Применить» тянуло бы мегабайты. За скачиванием следит
        # adblock.updater и кнопка в панели.
        try:
            blocklist_changed, blocklist_report = adblock.build(cfg, download=False)
            if blocklist_report.get("disabled"):
                steps.append("блокировщик рекламы выключен")
            else:
                steps.append(f"список блокировки: {blocklist_report['domains']} доменов")
            for source in blocklist_report.get("sources", []):
                if not source.get("ok"):
                    errors.append(f"список «{source['name']}»: {source.get('error')}")
        except Exception as exc:                       # noqa: BLE001
            blocklist_changed = False
            errors.append(f"список блокировки: {exc}")

        # Списки умного прокси — тоже из кеша; скачивает их proxylists.updater.
        try:
            proxy_changed, proxy_report = proxylists.build(cfg, download=False)
            if proxy_report.get("disabled"):
                steps.append("умный прокси выключен: в VPN идёт весь трафик")
            else:
                steps.append(f"умный прокси: {proxy_report['domains']} доменов, "
                             f"{proxy_report['subnets']} подсетей")
            for source in proxy_report.get("sources", []):
                if not source.get("ok"):
                    errors.append(f"список прокси «{source['name']}»: {source.get('error')}")
        except Exception as exc:                       # noqa: BLE001
            proxy_changed = False
            errors.append(f"списки умного прокси: {exc}")
        blocklist_changed = blocklist_changed or proxy_changed

        if dhcp_problems:
            # Писать конфиг с заведомо битым пулом нельзя: dnsmasq не стартует
            # и LAN останется без DHCP. Оставляем прошлый рабочий файл.
            dnsmasq_changed = False
            steps.append("конфиг dnsmasq не тронут: есть ошибки в настройках DHCP")
        else:
            dnsmasq_changed = dnsmasq_cfg.write(cfg) or blocklist_changed
            steps.append("конфиг dnsmasq " + ("обновлён" if dnsmasq_changed else "без изменений"))

        if restart_services:
            ok, err = dockerctl.restart(settings.XRAY_CONTAINER)
            tunnel.monitor.poke()
            steps.append("xray перезапущен" if ok else f"xray: {err}")
            if not ok:
                errors.append(f"перезапуск xray: {err}")
            if dnsmasq_changed:
                ok, err = dockerctl.restart(settings.DNSMASQ_CONTAINER)
                steps.append("dnsmasq перезапущен" if ok else f"dnsmasq: {err}")
                if not ok:
                    errors.append(f"перезапуск dnsmasq: {err}")

        _finish(not errors, steps, errors)
        return last_result()


def _finish(ok, steps, errors):
    _last_result.update({"ok": ok, "ts": int(time.time()),
                         "steps": steps, "errors": errors})


def xray_listening(port):
    """Дешёвая проба: TPROXY-инбаунд слушает порт?

    Смотрим таблицу сокетов, а не подключаемся: connect() на dokodemo-door
    с followRedirect xray принимает за петлю («loopback connection
    detected») — раз в секунду в лог и лишнее соединение внутри xray.
    """
    wanted = f":{port:04X}"
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as fh:
                next(fh, None)
                for line in fh:
                    fields = line.split()
                    # fields[1] — local_address:port, fields[3] — состояние (0A = LISTEN).
                    if len(fields) > 3 and fields[3] == "0A" and fields[1].endswith(wanted):
                        return True
        except OSError:
            continue
    return False


class Watchdog:
    """Держит TPROXY-роутинг в синхроне с реальным состоянием xray.

    Без него падение или рестарт xray оставляет LAN с правилами, ведущими в
    никуда: интернет пропадает на 5–15 секунд, пока xray грузит geo-базы.
    Watchdog снимает TPROXY за полсекунды — трафик уходит напрямую через WAN —
    и возвращает его обратно, как только xray снова слушает порт.
    """

    INTERVAL = 1.0

    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()
        self.state = "unknown"
        self.last_change = 0.0
        self._ticks = 0

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="watchdog", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _loop(self):
        while not self.stop_event.wait(self.INTERVAL):
            try:
                self._converge()
            except Exception:                          # noqa: BLE001
                log.exception("сбой в watchdog")

    def _converge(self):
        cfg = state.load()
        tproxy = cfg["tproxy"]
        # Ориентируемся на то, что реально лежит в конфиге xray: при откате на
        # direct галка «VPN включён» ещё стоит, а заворачивать уже некуда.
        xray_ready = (bool(cfg.get("vpn_enabled"))
                      and xray_build.effective()["vpn"]
                      and state.server_has_target(state.active_server(cfg))
                      and xray_listening(int(tproxy["port"])))
        # xray жив, но сервер за ним молчит (нода перезагружается, протокол
        # меняется, провайдер режет) — тоже уводим LAN напрямую, если это
        # не запрещено в настройках.
        failover = bool((cfg.get("failover") or {}).get("enabled", True))
        tunnel_ok = not failover or tunnel.monitor.healthy()
        want = xray_ready and tunnel_ok
        active = firewall.tproxy_active() and netctl.tproxy_routing_active(
            int(tproxy["fwmark"]), int(tproxy["table"]))

        if want and not active:
            netctl.tproxy_routing_up(int(tproxy["fwmark"]), int(tproxy["table"]))
            firewall.apply_tproxy(cfg, True)
            self._note("up", "туннель работает — включаю TPROXY")
        elif not want and active:
            firewall.apply_tproxy(cfg, False)
            netctl.tproxy_routing_down(int(tproxy["fwmark"]), int(tproxy["table"]))
            if xray_ready:
                self._note("failover", "туннель не отвечает — клиенты временно идут напрямую")
            else:
                self._note("down", "xray недоступен или VPN выключен — трафик напрямую")
        elif not want and xray_ready and self.state != "failover":
            self._note("failover", "туннель ещё не ответил — клиенты идут напрямую")

        # LAN-адрес мог снести networkd при событии на линке — возвращаем.
        lan = cfg["lan"]
        wanted = f"{lan['address']}/{lan['prefix']}"
        if wanted not in netctl.iface_addresses(lan["interface"]):
            netctl.configure_lan(lan["interface"], lan["address"], lan["prefix"])
            log.info("вернул адрес %s на %s", wanted, lan["interface"])

        # Перезапуск демона Docker пересоздаёт свои цепочки и вычищает наши
        # правила из DOCKER-USER — без них транзит мимо прокси снова умрёт.
        self._ticks += 1
        if self._ticks % 30 == 0 and not firewall.docker_user_ok(cfg):
            firewall.apply_docker_user(cfg)
            log.info("восстановил правила транзита в DOCKER-USER")

    def _note(self, new_state, message):
        if self.state != new_state:
            self.state = new_state
            self.last_change = time.time()
            log.info("watchdog: %s", message)


watchdog = Watchdog()

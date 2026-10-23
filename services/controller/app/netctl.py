"""Работа с сетевым стеком хоста: интерфейсы, sysctl, модули, policy routing.

Контейнер контроллера живёт в host network namespace и запущен privileged,
поэтому `ip`/`sysctl`/`modprobe` действуют ровно на хост.
"""

import ipaddress
import json
import logging
import os
from pathlib import Path

from . import settings
from .shell import out, run

log = logging.getLogger("circle.netctl")

# Модули, без которых не взлетит TPROXY. Часть из них может быть вкомпилирована
# в ядро — modprobe в этом случае просто молча отработает.
REQUIRED_MODULES = ["nf_tproxy_ipv4", "nft_tproxy", "nft_socket", "nf_socket_ipv4",
                    # flowtable: короткий путь для установленных соединений.
                    "nf_flow_table_inet", "nft_flow_offload",
                    # drivetemp отдаёт температуру SATA-дисков через hwmon;
                    # без него у них просто не будет датчика.
                    "drivetemp"]
PPPOE_MODULES = ["ppp_generic", "pppox", "pppoe"]


def modprobe(names):
    for name in names:
        run(["modprobe", name], check=False, timeout=15)


def sysctl_set(key, value):
    path = Path("/proc/sys") / key.replace(".", "/")
    try:
        if path.read_text().strip() == str(value):
            return True
        path.write_text(str(value))
        return True
    except OSError as exc:
        log.warning("не удалось выставить sysctl %s=%s: %s", key, value, exc)
        return False


def sysctl_get(key, default=None):
    try:
        return (Path("/proc/sys") / key.replace(".", "/")).read_text().strip()
    except OSError:
        return default


def prepare_host(lan_if, wan_if):
    """Один раз на старте: форвардинг, rp_filter и модули ядра."""
    modprobe(REQUIRED_MODULES)
    results = {
        "net.ipv4.ip_forward": sysctl_set("net.ipv4.ip_forward", 1),
        # rp_filter=1 (strict) убивает TPROXY: после `ip route local 0/0 dev lo`
        # обратный маршрут до источника выглядит «не с того» интерфейса, и ядро
        # дропает пакет ещё до того, как его увидит nftables.
        "net.ipv4.conf.all.rp_filter": sysctl_set("net.ipv4.conf.all.rp_filter", 0),
        "net.ipv4.conf.all.forwarding": sysctl_set("net.ipv4.conf.all.forwarding", 1),
    }
    for iface in filter(None, {lan_if, wan_if}):
        results[f"net.ipv4.conf.{iface}.rp_filter"] = sysctl_set(
            f"net.ipv4.conf.{iface}.rp_filter", 0
        )
    results["net.core.rps_sock_flow_entries"] = sysctl_set(
        "net.core.rps_sock_flow_entries", RPS_SOCK_FLOW_ENTRIES)
    for iface in filter(None, {lan_if, wan_if}):
        enable_rps(iface)
    return results


RPS_SOCK_FLOW_ENTRIES = 32768
RPS_FLOW_CNT = 4096


def iface_exists(name):
    return bool(name) and (Path("/sys/class/net") / name).exists()


def enable_rps(iface):
    """Раздать обработку входящих пакетов по всем ядрам.

    У встроенных сетевух ARM-плат одна аппаратная очередь, и без RPS весь
    softirq приёма (а с PPPoE — ещё и разбор сессии) висит на одном ядре,
    которое упирается в потолок задолго до гигабита. Ядро умеет хешировать
    и PPPoE-кадры, так что распределение работает и на WAN.
    """
    queues = Path("/sys/class/net") / iface / "queues"
    try:
        cpus = os.cpu_count() or 1
        mask = format((1 << cpus) - 1, "x")
        for rx in queues.glob("rx-*"):
            (rx / "rps_cpus").write_text(mask)
            (rx / "rps_flow_cnt").write_text(str(RPS_FLOW_CNT))
        return True
    except OSError as exc:
        log.warning("не удалось включить RPS на %s: %s", iface, exc)
        return False


def interfaces():
    """Список физических интерфейсов хоста с состоянием и адресами."""
    try:
        data = json.loads(out(["ip", "-j", "addr", "show"], default="[]") or "[]")
    except json.JSONDecodeError:
        return []
    result = []
    for link in data:
        name = link.get("ifname", "")
        if name == "lo" or name.startswith(("docker", "br-", "veth")):
            continue
        addrs = [
            f"{a['local']}/{a['prefixlen']}"
            for a in link.get("addr_info", [])
            if a.get("family") == "inet"
        ]
        result.append({
            "name": name,
            "mac": link.get("address", ""),
            "state": link.get("operstate", "UNKNOWN"),
            "carrier": has_carrier(name),
            "mtu": link.get("mtu"),
            "addresses": addrs,
        })
    return sorted(result, key=lambda i: i["name"])


def first_ppp():
    """Имя поднятой PPPoE-сессии: номер меняется от подключения к подключению."""
    try:
        names = sorted(p.name for p in Path("/sys/class/net").iterdir()
                       if p.name.startswith("ppp"))
    except OSError:
        return "ppp0"
    return names[0] if names else "ppp0"


def link_state(name):
    try:
        return Path(f"/sys/class/net/{name}/operstate").read_text().strip()
    except OSError:
        return "missing"


def has_carrier(name):
    try:
        return Path(f"/sys/class/net/{name}/carrier").read_text().strip() == "1"
    except OSError:
        return False


def iface_addresses(name):
    try:
        data = json.loads(out(["ip", "-j", "-4", "addr", "show", "dev", name],
                              default="[]") or "[]")
    except json.JSONDecodeError:
        return []
    if not data:
        return []
    return [f"{a['local']}/{a['prefixlen']}" for a in data[0].get("addr_info", [])
            if a.get("family") == "inet"]


def iface_ip(name):
    addrs = iface_addresses(name)
    return addrs[0].split("/")[0] if addrs else ""


def default_gateway():
    try:
        routes = json.loads(out(["ip", "-j", "route", "show", "default"],
                                default="[]") or "[]")
    except json.JSONDecodeError:
        return {}
    if not routes:
        return {}
    return {"gateway": routes[0].get("gateway", ""), "dev": routes[0].get("dev", "")}


def mark_unmanaged(iface):
    """Просим systemd-networkd не трогать LAN-порт.

    netplan генерит /run/systemd/network/10-netplan-<if>.network; наш файл с
    префиксом 05- сортируется раньше и выигрывает.
    """
    if not settings.HOST_NETWORKD_DIR.is_dir():
        return False
    target = settings.HOST_NETWORKD_DIR / "05-circle-lan-unmanaged.network"
    body = (
        "# Сгенерировано circle-router: LAN-портом управляет контроллер.\n"
        f"[Match]\nName={iface}\n\n[Link]\nUnmanaged=yes\n"
    )
    try:
        if target.exists() and target.read_text() == body:
            return True
        target.write_text(body)
    except OSError as exc:
        log.warning("не удалось записать %s: %s", target, exc)
        return False
    run(["networkctl", "reload"], check=False, timeout=10)
    return True


def configure_lan(iface, address, prefix):
    """Идемпотентно вешаем на LAN-порт статический адрес и поднимаем линк."""
    wanted = f"{address}/{prefix}"
    network = ipaddress.ip_network(wanted, strict=False)
    current = iface_addresses(iface)

    for addr in current:
        if addr == wanted:
            continue
        try:
            same_net = ipaddress.ip_interface(addr).network == network
        except ValueError:
            same_net = False
        # Адреса из чужой подсети могли повесить не мы — не трогаем. А остатки
        # прошлой конфигурации той же подсети убираем, чтобы не копились.
        if same_net:
            run(["ip", "addr", "del", addr, "dev", iface], check=False)

    if wanted not in current:
        run(["ip", "addr", "replace", wanted, "dev", iface], check=False)
    run(["ip", "link", "set", iface, "up"], check=False)
    return wanted


def tproxy_routing_up(fwmark, table):
    """fwmark -> отдельная таблица, где весь мир «локальный» (dev lo).

    Именно это позволяет TPROXY перехватить транзитный пакет.
    """
    # Дубли обязательно вычищаем: иначе каждое «Применить» плодит правила.
    while run(["ip", "rule", "del", "fwmark", str(fwmark), "table", str(table)],
              check=False)[0] == 0:
        pass
    run(["ip", "rule", "add", "fwmark", str(fwmark), "table", str(table)], check=False)
    run(["ip", "route", "replace", "local", "0.0.0.0/0", "dev", "lo",
         "table", str(table)], check=False)


def tproxy_routing_down(fwmark, table):
    while run(["ip", "rule", "del", "fwmark", str(fwmark), "table", str(table)],
              check=False)[0] == 0:
        pass
    run(["ip", "route", "flush", "table", str(table)], check=False)


def tproxy_routing_active(fwmark=1, table=100):
    return f"fwmark {hex(fwmark)} lookup {table}" in out(["ip", "rule", "show"])


def host_uptime():
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def loadavg():
    try:
        return list(os.getloadavg())
    except OSError:
        return [0.0, 0.0, 0.0]

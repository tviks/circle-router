"""Работа с сетевым стеком хоста: интерфейсы, sysctl, модули, policy routing.

Контейнер контроллера живёт в host network namespace и запущен privileged,
поэтому `ip`/`sysctl`/`modprobe` действуют ровно на хост.
"""

import ipaddress
import json
import logging
import os
import re
from pathlib import Path

from . import settings
from .shell import out, run

log = logging.getLogger("circle.netctl")

# Бридж, в который собираются LAN-порт и включённые в свич свободные порты.
# Присутствует в системе только когда свич-порты включены; иначе адрес живёт
# на физическом LAN-порте, как раньше.
LAN_BRIDGE = "br-lan"

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


def prepare_host(lan_if, wan_if, lan_ports=()):
    """Один раз на старте: форвардинг, rp_filter и модули ядра.

    lan_if — девайс с адресом LAN (физический порт или бридж), lan_ports —
    все физические порты LAN: rp_filter нужен и на бридже, и на его членах,
    а RPS имеет смысл включать только на физических портах — входящие пакеты
    размечаются на них, до бриджа уже не доходит очередь.
    """
    modprobe(REQUIRED_MODULES)
    results = {
        "net.ipv4.ip_forward": sysctl_set("net.ipv4.ip_forward", 1),
        # rp_filter=1 (strict) убивает TPROXY: после `ip route local 0/0 dev lo`
        # обратный маршрут до источника выглядит «не с того» интерфейса, и ядро
        # дропает пакет ещё до того, как его увидит nftables.
        "net.ipv4.conf.all.rp_filter": sysctl_set("net.ipv4.conf.all.rp_filter", 0),
        "net.ipv4.conf.all.forwarding": sysctl_set("net.ipv4.conf.all.forwarding", 1),
    }
    for iface in filter(None, {lan_if, wan_if, *lan_ports}):
        results[f"net.ipv4.conf.{iface}.rp_filter"] = sysctl_set(
            f"net.ipv4.conf.{iface}.rp_filter", 0
        )
    results["net.core.rps_sock_flow_entries"] = sysctl_set(
        "net.core.rps_sock_flow_entries", RPS_SOCK_FLOW_ENTRIES)
    for iface in filter(None, {wan_if, *lan_ports}):
        enable_rps(iface)
    return results


RPS_SOCK_FLOW_ENTRIES = 32768
RPS_FLOW_CNT = 4096


def iface_exists(name):
    return bool(name) and (Path("/sys/class/net") / name).exists()


def lan_members(cfg):
    """Все физические порты, которые должны работать как LAN.

    Главный LAN-порт всегда первый; свич-порты — по порядку из конфига.
    Дубликаты и пустые строки выкидываем: конфиг приходит из панели и может
    содержать что угодно.
    """
    lan = cfg["lan"]
    members = [lan["interface"]]
    for port in lan.get("switch_ports") or []:
        port = (port or "").strip()
        if port and port not in members:
            members.append(port)
    return members


def lan_device(cfg):
    """Девайс, на котором живёт адрес LAN и к которому привязан dnsmasq.

    Свич-порты включены — бридж (транзит между портами идёт на L2, мимо
    роутера); нет — физический порт, как и раньше: на платах с двумя
    разъёмами бридж не нужен вовсе.
    """
    if len(lan_members(cfg)) > 1:
        return LAN_BRIDGE
    return cfg["lan"]["interface"]


def bridge_members(bridge=LAN_BRIDGE):
    """Текущие порты бриджа (содержимое /sys/class/net/<br>/brif)."""
    try:
        return sorted(p.name for p in (Path("/sys/class/net") / bridge / "brif").iterdir())
    except OSError:
        return []


def bridge_ok(cfg):
    """Дешёвая проверка для watchdog: бридж есть и собран из нужных портов?

    Несуществующие порты не ждём: выдернутый из USB адаптер иначе делал бы
    проверку вечно ложной, и watchdog раз в секунду гонял бы ip зря.
    """
    if not iface_exists(LAN_BRIDGE):
        return False
    wanted = sorted(p for p in lan_members(cfg) if iface_exists(p))
    return bridge_members() == wanted


def apply_bridge(cfg):
    """Собирает свич-порты в бридж. Возвращает имя LAN-девайса.

    Идемпотентно: недостающие порты добавляются, лишние вычищаются. Когда
    свич не нужен, бридж удаляется — освободившиеся порты продолжают работать
    сами по себе.
    """
    members = lan_members(cfg)
    if len(members) == 1:
        if iface_exists(LAN_BRIDGE):
            run(["ip", "link", "del", LAN_BRIDGE], check=False)
        return members[0]

    if not iface_exists(LAN_BRIDGE):
        run(["ip", "link", "add", LAN_BRIDGE, "type", "bridge"], check=False)
    for port in members:
        if iface_exists(port) and port not in bridge_members():
            run(["ip", "link", "set", port, "master", LAN_BRIDGE], check=False)
    for port in bridge_members():
        if port not in members:
            run(["ip", "link", "set", port, "nomaster"], check=False)
    for port in members:
        run(["ip", "link", "set", port, "up"], check=False)
    run(["ip", "link", "set", LAN_BRIDGE, "up"], check=False)
    return LAN_BRIDGE


def prune_member_addresses(members, network, keep=None):
    """Сгоняет с физических портов LAN адреса нашей подсети.

    Пока порт был LAN-портом (или жил в бридже), на нём мог остаться адрес
    роутера — теперь он мешает: ядро отвечает на ARP с обоих девайсов
    (ARP flux). Адреса чужих подсетей не трогаем, как и в configure_lan.
    keep — девайс, на котором адрес сейчас живёт (его не трогаем).
    """
    for port in members:
        if port in (keep, LAN_BRIDGE) or not iface_exists(port):
            continue
        for addr in iface_addresses(port):
            try:
                same_net = ipaddress.ip_interface(addr).network == network
            except ValueError:
                same_net = False
            if same_net:
                run(["ip", "addr", "del", addr, "dev", port], check=False)


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
        # br-<12 hex> — бриджи Docker-сетей; наш br-lan (имя короче и без hex)
        # в панель показываем: это легальный LAN-девайс.
        if name == "lo" or name.startswith(("docker", "veth")) \
                or re.fullmatch(r"br-[0-9a-f]{12}", name):
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


def mark_unmanaged(ifaces):
    """Просим systemd-networkd не трогать LAN-порты.

    netplan генерит /run/systemd/network/10-netplan-<if>.network; наш файл с
    префиксом 05- сортируется раньше и выигрывает. Свич-порты помечаем тем же
    файлом (Match Name принимает список): иначе networkd, найдя у порта
    конфиг netplan'а, вынул бы его из нашего бриджа.
    """
    names = ",".join(dict.fromkeys(i for i in ifaces if i))
    if not names or not settings.HOST_NETWORKD_DIR.is_dir():
        return False
    target = settings.HOST_NETWORKD_DIR / "05-circle-lan-unmanaged.network"
    body = (
        "# Сгенерировано circle-router: LAN-портами управляет контроллер.\n"
        f"[Match]\nName={names}\n\n[Link]\nUnmanaged=yes\n"
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


# У kernel-интерфейсов скромные лимиты на длину и состав имени (IFNAMSIZ 16
# с нулём на конце), а шире набор и не нужен.
_IFACE_RE = re.compile(r"^[a-zA-Z0-9._@:-]{1,15}$")


def validate_lan(cfg):
    """Претензии к настройке LAN: какие порты можно включать в свич."""
    problems = []
    lan = cfg["lan"]
    seen = {lan["interface"]}
    for port in lan.get("switch_ports") or []:
        port = (port or "").strip()
        if not port:
            continue
        if not _IFACE_RE.match(port):
            problems.append(f"свич-порт «{port}»: не похоже на имя интерфейса")
            continue
        if port == cfg["wan"]["interface"]:
            problems.append(f"{port}: WAN-порт нельзя включать в LAN-свич")
            continue
        if port in seen:
            problems.append(f"{port}: порт уже занят в LAN")
            continue
        seen.add(port)
    return problems


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

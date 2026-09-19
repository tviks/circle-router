"""Чтение/запись router.json — единственного источника правды о конфигурации."""

import ipaddress
import json
import os
import threading
import uuid

from . import settings

_lock = threading.RLock()


def _default_config():
    lan_net = ipaddress.ip_network(
        f"{settings.DEFAULTS['lan_address']}/{settings.DEFAULTS['lan_prefix']}",
        strict=False,
    )
    hosts = list(lan_net.hosts())
    return {
        "version": 1,
        "vpn_enabled": False,
        "servers": [],
        "active_server": None,
        "bypass": {
            "geosite": ["geosite:private", "geosite:category-ru", "geosite:cn"],
            "geoip": ["geoip:private", "geoip:ru", "geoip:cn"],
            "domains": [],
            "ips": [],
        },
        "force_proxy": {"geosite": [], "domains": [], "ips": []},
        "wan": {
            "interface": settings.DEFAULTS["wan_if"],
            "mode": "dhcp",
            "static": {"address": "", "gateway": "", "dns": []},
            "pppoe": {
                "username": "",
                "password": "",
                "service": "",
                "mtu": 1492,
                "lcp_echo_interval": 20,
                "lcp_echo_failure": 3,
            },
        },
        "lan": {
            "interface": settings.DEFAULTS["lan_if"],
            "address": settings.DEFAULTS["lan_address"],
            "prefix": settings.DEFAULTS["lan_prefix"],
        },
        "dhcp": {
            "enabled": True,
            "range_start": str(hosts[99]) if len(hosts) > 99 else str(hosts[0]),
            "range_end": str(hosts[199]) if len(hosts) > 199 else str(hosts[-1]),
            "lease_time": "12h",
            "domain": "circle.lan",
            "dns_servers": [],
            "reservations": [],
        },
        "dns": {
            "upstream": ["1.1.1.1", "8.8.8.8"],
            "cache_size": 2048,
            "log_queries": False,
            # dnsmasq спрашивает не провайдера напрямую, а DNS-инбаунд xray,
            # чтобы резолвинг ходил по тем же правилам, что и трафик.
            "route_via_vpn": True,
        },
        "tproxy": {"port": 12345, "fwmark": 1, "table": 100},
        "firewall": {
            "allow_wan_ssh": True,
            "allow_wan_admin": True,
            "admin_port": settings.HTTP_PORT,
            "extra_wan_tcp_ports": [],
        },
        "port_forward": {
            "enabled": True,
            # {"id", "name", "enabled", "protocol": tcp|udp|both,
            #  "src_port", "dest_ip", "dest_port", "source"}
            "rules": [],
        },
        "uplink_access": {
            # Клиенты LAN видят сеть, в которую воткнут WAN-порт (NAS, принтер,
            # камеры домашнего роутера). Работает через NAT, маршруты не нужны.
            "lan_to_uplink": True,
            # Обратное направление: хосты из сети аплинка сами стучатся в LAN.
            # Нужен статический маршрут на роутере аплинка.
            "uplink_to_lan": False,
            # Не маскарадить трафик в подсеть аплинка, чтобы её хосты видели
            # настоящие адреса клиентов LAN. Тоже требует маршрута.
            "no_nat_to_uplink": False,
            # Пусто — подсети определяются по адресам WAN-интерфейса.
            "networks": [],
        },
        "adblock": {
            "enabled": True,
            "sources": [
                {"name": "AdGuard DNS filter", "enabled": True,
                 "url": "https://adguardteam.github.io/HostlistsRegistry/assets/filter_1.txt"},
                {"name": "AdAway (мобильная реклама)", "enabled": True,
                 "url": "https://adguardteam.github.io/HostlistsRegistry/assets/filter_2.txt"},
                {"name": "StevenBlack hosts (реклама + вредонос)", "enabled": False,
                 "url": "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"},
                # oisd отдаёт список медленно и часто рвёт соединение — держим
                # выключенным, включайте на свой страх.
                {"name": "OISD small", "enabled": False,
                 "url": "https://small.oisd.nl/dnsmasq2"},
            ],
            "allowlist": [],
            "blocklist": [],
            "auto_update_hours": 24,
            "count_queries": True,
        },
        "speedtest": {
            # Cloudflare есть почти везде и отдаёт заданный объём. Больше
            # ~50 МБ за запрос он не даёт (403), поэтому качаем в цикле.
            "url": "https://speed.cloudflare.com/__down?bytes=50000000",
            "duration": 8,
        },
        "block_quic": True,
        "log_level": "warning",
    }


def _deep_fill(target, template):
    """Дописывает недостающие ключи из шаблона, не трогая уже заданные."""
    for key, value in template.items():
        if key not in target:
            target[key] = json.loads(json.dumps(value))
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_fill(target[key], value)
    return target


def new_id():
    return uuid.uuid4().hex[:12]


def load():
    with _lock:
        if not settings.CONFIG_PATH.exists():
            cfg = _default_config()
            _write(cfg)
            return cfg
        try:
            cfg = json.loads(settings.CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        cfg = _deep_fill(cfg, _default_config())
        return cfg


def _write(cfg):
    tmp = settings.CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, settings.CONFIG_PATH)


def save(cfg):
    with _lock:
        _write(cfg)
        return cfg


def update(mutator):
    """Атомарно применяет функцию-мутатор к конфигу и сохраняет результат."""
    with _lock:
        cfg = load()
        mutator(cfg)
        _write(cfg)
        return cfg


def active_server(cfg):
    for s in cfg.get("servers") or []:
        if s.get("id") == cfg.get("active_server"):
            return s
    return None


def server_has_target(srv):
    if not srv:
        return False
    if (srv.get("url") or "").strip():
        return True
    return isinstance(srv.get("outbound"), dict) and bool(srv["outbound"])


def lan_network(cfg):
    lan = cfg["lan"]
    return ipaddress.ip_network(f"{lan['address']}/{lan['prefix']}", strict=False)


def wan_interface(cfg):
    """Интерфейс, через который реально уходит трафик наружу.

    В режиме PPPoE это ppp-интерфейс, а физический порт остаётся только
    несущей. Номер не фиксирован: после переподключения сессия может подняться
    как ppp1, ppp2 и так далее, поэтому спрашиваем маршрут по умолчанию.
    """
    if cfg["wan"].get("mode") == "pppoe":
        from . import netctl
        device = (netctl.default_gateway() or {}).get("dev", "")
        return device if device.startswith("ppp") else "ppp0"
    return cfg["wan"]["interface"]


def wan_egress_pattern(cfg):
    """То же самое, но для nftables: там можно обойтись шаблоном.

    `ppp*` накрывает любую сессию и переживает переподключение без пересборки
    правил.
    """
    if cfg["wan"].get("mode") == "pppoe":
        return "ppp*"
    return cfg["wan"]["interface"]

"""Генерация dnsmasq.conf и чтение выданных аренд."""

import ipaddress
import logging
import os
import time

from . import settings, state, xray_build

log = logging.getLogger("circle.dnsmasq")


def render(cfg):
    lan = cfg["lan"]
    dhcp = cfg["dhcp"]
    dns = cfg["dns"]
    network = state.lan_network(cfg)
    router_ip = lan["address"]

    lines = [
        "# Сгенерировано circle-router. Правки руками перезапишутся.",
        f"interface={lan['interface']}",
        f"listen-address={router_ip}",
        "bind-interfaces",
        f"except-interface={cfg['wan']['interface']}",
        f"no-dhcp-interface={cfg['wan']['interface']}",
        "",
        "# ── DNS ──────────────────────────────────────────────────────────────",
        "no-resolv",
        "domain-needed",
        "bogus-priv",
        f"cache-size={int(dns.get('cache_size') or 2048)}",
    ]

    if dns.get("route_via_vpn", True):
        # Upstream — DNS-инбаунд xray: резолвинг идёт по тем же правилам
        # маршрутизации, что и трафик, и не утекает мимо VPN.
        lines.append(f"server=127.0.0.1#{xray_build.DNS_INBOUND_PORT}")
    else:
        for upstream in dns.get("upstream") or ["1.1.1.1"]:
            lines.append(f"server={upstream}")

    if dhcp.get("domain"):
        lines += [f"domain={dhcp['domain']}", "expand-hosts", "local=/%s/" % dhcp["domain"]]

    adblock = cfg.get("adblock") or {}
    if adblock.get("enabled", True):
        lines += [
            "",
            "# Блокировщик рекламы: список генерируется отдельно и подключается сюда.",
            f"conf-file={settings.BLOCKLIST_IN_CONTAINER}",
        ]

    # Логи всегда в stderr: их забирает docker, а значит ротацию делает
    # json-file драйвер, и нам не нужно сторожить размер файла самим.
    lines.append("log-facility=-")
    if dns.get("log_queries") or adblock.get("count_queries", True):
        # Без log-queries нечего считать: статистика блокировок берётся
        # из строк «config <домен> is 0.0.0.0».
        lines.append("log-queries=extra")

    lines += ["", "# ── DHCP ─────────────────────────────────────────────────────────────"]
    if dhcp.get("enabled", True):
        lease = dhcp.get("lease_time") or "12h"
        lines += [
            f"dhcp-range={dhcp['range_start']},{dhcp['range_end']},"
            f"{network.netmask},{lease}",
            f"dhcp-option=option:router,{router_ip}",
            f"dhcp-option=option:dns-server,"
            f"{','.join(dhcp.get('dns_servers') or [router_ip])}",
            f"dhcp-leasefile={settings.DNSMASQ_LEASES_IN_CONTAINER}",
            "dhcp-authoritative",
            "read-ethers",
        ]
        for res in dhcp.get("reservations") or []:
            mac = (res.get("mac") or "").strip()
            ip = (res.get("ip") or "").strip()
            if not mac or not ip:
                continue
            host = (res.get("hostname") or "").strip()
            parts = [mac] + ([host] if host else []) + [ip]
            lines.append("dhcp-host=" + ",".join(parts))
    else:
        lines.append("# DHCP выключен в панели")

    return "\n".join(lines) + "\n"


def write(cfg):
    """Возвращает True, если конфиг изменился (значит, нужен рестарт dnsmasq)."""
    body = render(cfg)
    path = settings.DNSMASQ_CONFIG_PATH
    if path.exists() and path.read_text(encoding="utf-8") == body:
        return False
    tmp = path.with_suffix(".conf.tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return True


def leases():
    path = settings.DNSMASQ_LEASES
    if not path.exists():
        return []
    now = int(time.time())
    result = []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            expires_at = int(parts[0])
        except ValueError:
            continue
        result.append({
            "mac": parts[1],
            "ip": parts[2],
            "hostname": "" if parts[3] == "*" else parts[3],
            "expires_in": max(0, expires_at - now),
        })
    return result


def validate(cfg):
    """Проверки, которые дешевле сделать до записи конфига, чем ловить в логах."""
    errors = []
    try:
        network = state.lan_network(cfg)
    except ValueError as exc:
        return [f"некорректный адрес LAN: {exc}"]

    dhcp = cfg["dhcp"]
    if dhcp.get("enabled", True):
        for key in ("range_start", "range_end"):
            try:
                addr = ipaddress.ip_address(dhcp[key])
            except ValueError:
                errors.append(f"{key}: некорректный IP «{dhcp.get(key)}»")
                continue
            if addr not in network:
                errors.append(f"{key}: {addr} вне подсети {network}")
        try:
            if ipaddress.ip_address(dhcp["range_start"]) > ipaddress.ip_address(dhcp["range_end"]):
                errors.append("начало пула DHCP больше его конца")
        except ValueError:
            pass
        for res in dhcp.get("reservations") or []:
            try:
                if ipaddress.ip_address(res.get("ip", "")) not in network:
                    errors.append(f"резервация {res.get('ip')} вне подсети {network}")
            except ValueError:
                errors.append(f"резервация: некорректный IP «{res.get('ip')}»")
    return errors

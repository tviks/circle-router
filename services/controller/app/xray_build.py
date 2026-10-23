"""Сборка /etc/xray/config.json из router.json."""

import json
import logging
import os
import time

from . import settings, state, xray_url
from .shell import run

log = logging.getLogger("circle.xray")

# Локальный DNS-инбаунд, в который смотрит dnsmasq. Благодаря ему резолвинг
# уходит по тем же правилам маршрутизации, что и остальной трафик, а не мимо
# VPN напрямую с WAN-адреса.
DNS_INBOUND_PORT = 5300
# Тег, под которым в роутинг попадают запросы встроенного резолвера xray.
DNS_CLIENT_TAG = "dns-internal"
DNS_BALANCER = "dns-via-vpn"


def _dns_failover(cfg):
    """Резолвер уходит на direct при мёртвом туннеле, если не включён kill switch."""
    return bool((cfg.get("failover") or {}).get("enabled", True))

# HTTP-инбаунд монитора туннеля (см. tunnel.py). Только на loopback.
HEALTH_PORT = 10809

# Что реально лежит в /etc/xray/config.json. Отличается от cfg["vpn_enabled"],
# когда конфиг с сервером не прошёл проверку и мы откатились на direct.
_effective = {"vpn": False, "error": None, "ts": 0}


def effective():
    return dict(_effective)


def outbound_for(server):
    """Ссылка приоритетнее сырого outbound: её пользователь может править в UI."""
    if not server:
        return None
    url = (server.get("url") or "").strip()
    if url:
        return xray_url.parse_url(url)
    raw = server.get("outbound")
    if isinstance(raw, dict) and raw:
        return xray_url.normalize_outbound(raw)
    return None


def extra_outbounds_for(server):
    """Вспомогательные аутбаунды сервера (у своих нод — фрагментатор
    ClientHello, через который основной аутбаунд устанавливает TCP)."""
    result = []
    for raw in (server or {}).get("extra_outbounds") or []:
        if isinstance(raw, dict) and raw.get("tag"):
            extra = json.loads(json.dumps(raw))
            sockopt = (extra.get("streamSettings") or {}).get("sockopt")
            if isinstance(sockopt, dict):
                sockopt.pop("mark", None)
            result.append(extra)
    return result


def routing_rules(cfg, vpn_enabled):
    rules = [{"type": "field", "inboundTag": ["api-in"], "outboundTag": "api"}]
    if vpn_enabled:
        # Служебный инбаунд монитора туннеля всегда уходит в proxy — мимо
        # правил обхода, иначе проверка мерила бы прямой канал.
        rules.append({"type": "field", "inboundTag": ["health-in"], "outboundTag": "proxy"})

    if not vpn_enabled:
        rules.append({"type": "field", "inboundTag": ["dns-in"], "outboundTag": "dns-out"})
        return rules

    # Собственные запросы резолвера xray — строго через VPN. Иначе они
    # попадают под правило «UDP напрямую», а провайдер подменяет ответы даже
    # от 8.8.8.8: на заблокированные домены прилетает NXDOMAIN.
    if _dns_failover(cfg):
        # …но если туннель умер, резолвер не должен умереть вместе с ним:
        # балансировщик с одним участником и запасным direct. Иначе в обходе
        # (tunnel.py) у клиентов перестают резолвиться домены из списков, а в
        # режиме «весь DNS через VPN» — вообще все.
        rules.append({"type": "field", "inboundTag": [DNS_CLIENT_TAG],
                      "balancerTag": DNS_BALANCER})
    else:
        rules.append({"type": "field", "inboundTag": [DNS_CLIENT_TAG], "outboundTag": "proxy"})

    # DNS-запросы (и локальный инбаунд, и всё, что клиенты шлют на чужой :53)
    # отдаём встроенному резолверу xray.
    rules.append({"type": "field", "inboundTag": ["dns-in"], "outboundTag": "dns-out"})
    rules.append({"type": "field", "inboundTag": ["tproxy-in"], "port": "53",
                  "outboundTag": "dns-out"})

    if cfg.get("block_quic", True):
        # VLESS поверх TCP не тянет UDP без xudp, поэтому QUIC режем — браузеры
        # быстро откатываются на TCP, и он уже идёт через VPN.
        rules.append({"type": "field", "network": "udp", "port": "443",
                      "outboundTag": "block"})
    # Остальной UDP (NTP, игры, discovery) пускаем напрямую через NAT.
    rules.append({"type": "field", "network": "udp", "outboundTag": "direct"})

    # Свои же подсети и служебные адреса — всегда мимо прокси.
    rules.append({
        "type": "field",
        "ip": [str(state.lan_network(cfg)), "224.0.0.0/4", "255.255.255.255/32"],
        "outboundTag": "direct",
    })

    force = cfg.get("force_proxy") or {}
    fp_domains = list(force.get("geosite") or []) + list(force.get("domains") or [])
    if fp_domains:
        rules.append({"type": "field", "domain": fp_domains, "outboundTag": "proxy"})
    if force.get("ips"):
        rules.append({"type": "field", "ip": list(force["ips"]), "outboundTag": "proxy"})

    bypass = cfg.get("bypass") or {}
    bp_domains = list(bypass.get("geosite") or []) + list(bypass.get("domains") or [])
    if bp_domains:
        rules.append({"type": "field", "domain": bp_domains, "outboundTag": "direct"})
    bp_ips = list(bypass.get("geoip") or []) + list(bypass.get("ips") or [])
    if bp_ips:
        rules.append({"type": "field", "ip": bp_ips, "outboundTag": "direct"})

    return rules


def build(cfg):
    server = state.active_server(cfg)
    proxy_outbound = None
    error = None
    if state.server_has_target(server):
        try:
            proxy_outbound = outbound_for(server)
        except Exception as exc:            # noqa: BLE001 — показываем текст в UI
            error = str(exc)
            log.warning("не удалось собрать outbound: %s", exc)

    vpn_enabled = bool(cfg.get("vpn_enabled")) and proxy_outbound is not None
    tproxy_port = int(cfg["tproxy"]["port"])
    upstream = [d for d in (cfg.get("dns") or {}).get("upstream") or [] if d] or ["1.1.1.1"]

    inbounds = [
        {
            "tag": "api-in",
            "listen": "127.0.0.1",
            "port": 10085,
            "protocol": "dokodemo-door",
            "settings": {"address": "127.0.0.1"},
        },
        {
            # ВАЖНО: никакого `mark` в sockopt. Принятые сокеты унаследовали бы
            # SO_MARK, ответы клиентам попали бы в policy-route на lo и
            # зациклились внутри xray.
            "tag": "tproxy-in",
            "port": tproxy_port,
            "protocol": "dokodemo-door",
            "settings": {"network": "tcp,udp", "followRedirect": True},
            "streamSettings": {"sockopt": {"tproxy": "tproxy"}},
            "sniffing": {"enabled": True,
                         "destOverride": ["http", "tls", "quic"],
                         "routeOnly": True},
        },
        {
            "tag": "dns-in",
            "listen": "127.0.0.1",
            "port": DNS_INBOUND_PORT,
            "protocol": "dokodemo-door",
            "settings": {"address": upstream[0], "port": 53, "network": "tcp,udp"},
        },
    ]
    if vpn_enabled:
        inbounds.append({
            "tag": "health-in",
            "listen": "127.0.0.1",
            "port": HEALTH_PORT,
            "protocol": "http",
            "settings": {"allowTransparent": False},
        })

    outbounds = []
    # Никакого sockopt.mark на аутбаунде: иначе собственные пакеты xray уйдут
    # в ту же policy-таблицу и закольцуются.
    if proxy_outbound and vpn_enabled:
        outbounds.append(proxy_outbound)
        outbounds.extend(extra_outbounds_for(server))
    outbounds.append({"tag": "direct", "protocol": "freedom",
                      "settings": {"domainStrategy": "UseIP"}})
    outbounds.append({"tag": "block", "protocol": "blackhole"})
    outbounds.append({"tag": "dns-out", "protocol": "dns"})

    if not vpn_enabled:
        # xray берёт первый аутбаунд как дефолтный.
        outbounds.sort(key=lambda o: 0 if o["tag"] == "direct" else 1)

    # В умном режиме в xray попадают только адреса, уже отобранные DNS-списком,
    # так что IPIfNonMatch лишь добавлял бы DNS-запрос на каждое соединение,
    # у которого сниффнутый домен не совпал с правилами. AsIs матчит ip-правила
    # по настоящему адресу назначения — ровно то, что нужно.
    smart = vpn_enabled and bool((cfg.get("smart_proxy") or {}).get("enabled"))
    domain_strategy = "AsIs" if smart else "IPIfNonMatch"

    config = {
        "log": {
            "loglevel": (cfg.get("log_level") or "warning"),
            "error": "/var/log/xray/error.log",
        },
        "api": {"tag": "api", "services": ["StatsService"]},
        "stats": {},
        "policy": {
            "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}},
            "system": {
                "statsInboundUplink": True, "statsInboundDownlink": True,
                "statsOutboundUplink": True, "statsOutboundDownlink": True,
            },
        },
        # Без "localhost": системный резолвер хоста смотрит в наш же dnsmasq,
        # а тот шлёт домены из списков обратно в xray — петля и таймауты.
        "dns": {"servers": upstream, "queryStrategy": "UseIPv4",
                "tag": DNS_CLIENT_TAG},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"domainStrategy": domain_strategy,
                    **({"balancers": [{"tag": DNS_BALANCER, "selector": ["proxy"],
                                       "fallbackTag": "direct",
                                       "strategy": {"type": "leastPing"}}]}
                       if vpn_enabled and _dns_failover(cfg) else {}),
                    "rules": routing_rules(cfg, vpn_enabled)},
    }
    if vpn_enabled and _dns_failover(cfg):
        # Observatory решает, жив ли proxy для балансировщика резолвера. До
        # первой пробы (секунды после рестарта) запросы идут через direct.
        probe = ((cfg.get("failover") or {}).get("probe_urls") or [None])[0]
        config["observatory"] = {
            "subjectSelector": ["proxy"],
            "probeUrl": probe or "https://www.gstatic.com/generate_204",
            "probeInterval": "10s",
            "enableConcurrency": True,
        }
    return config, vpn_enabled, error


def _first_error_line(output):
    for line in output.splitlines():
        if line.startswith("Failed to start:") or "> infra/conf:" in line:
            return line.split("Failed to start: ")[-1].strip()
    return output.strip().splitlines()[-1] if output.strip() else "неизвестная ошибка"


def validate(path):
    """`xray run -test` до перезапуска: битый сервер иначе уводит xray в
    crash-loop, и понять причину можно только по логам контейнера."""
    rc, output = run(
        # -format обязателен: формат xray определяет по расширению файла,
        # а у кандидата оно своё.
        [settings.XRAY_BIN, "run", "-test", "-config", str(path), "-format=json"],
        check=False,
        timeout=45,
        # Без этого -test не найдёт geosite.dat и упадёт на правилах geosite:*.
        env={"XRAY_LOCATION_ASSET": str(settings.GEO_DIR)},
    )
    return rc == 0, output


def write(cfg):
    """Пишет конфиг xray. Возвращает (vpn_enabled, error).

    Если конфиг с VPN не проходит проверку, откатываемся на прямой роутинг:
    интернет в LAN важнее, чем упорство с нерабочим сервером.
    """
    config, vpn_enabled, error = build(cfg)
    tmp = settings.XRAY_CONFIG_PATH.parent / "config.candidate.json"

    def dump(payload):
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    dump(config)
    ok, output = validate(tmp)

    if not ok and vpn_enabled:
        error = _first_error_line(output)
        log.warning("конфиг с VPN не прошёл проверку (%s), откатываюсь на direct", error)
        fallback = dict(cfg)
        fallback["vpn_enabled"] = False
        config, vpn_enabled, _ = build(fallback)
        dump(config)
        ok, output = validate(tmp)
    if not ok:
        # Даже direct-конфиг не взлетел — оставляем прошлый рабочий файл.
        tmp.unlink(missing_ok=True)
        error = _first_error_line(output)
        _effective.update({"vpn": False, "error": error, "ts": int(time.time())})
        return False, error

    os.replace(tmp, settings.XRAY_CONFIG_PATH)
    _effective.update({"vpn": vpn_enabled, "error": error, "ts": int(time.time())})
    return vpn_enabled, error

"""Генерация и применение nftables-правил.

Принципиальный момент: мы НИКОГДА не делаем `flush ruleset`. Docker держит свои
цепочки в таблицах `filter`/`nat`, и общий flush снёс бы их вместе с сетью
контейнеров. Вместо этого всё живёт в собственных таблицах circle_*, которые
удаляются и создаются заново одной атомарной транзакцией `nft -f`.
"""

import ipaddress
import json
import logging

from . import netctl, settings, state
from .shell import out, run

log = logging.getLogger("circle.firewall")

# Диапазоны, которые никогда не заворачиваем в прокси: свои подсети, loopback,
# link-local, multicast. 240.0.0.0/4 уже включает 255.255.255.255/32 — если
# добавить его отдельно, nft ругнётся conflicting intervals.
BYPASS_NETS = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
]


def _nft(script):
    return run(["nft", "-f", "-"], input_text=script, timeout=30)


def _add_uncovered(nets, candidate):
    """Добавляет подсеть, только если её ещё не накрывает что-то из списка.

    Пересекающиеся интервалы в одном nft-сете — ошибка `conflicting intervals`.
    """
    network = ipaddress.ip_network(str(candidate), strict=False)
    for existing in nets:
        if network.subnet_of(ipaddress.ip_network(existing)):
            return nets
    nets.append(str(network))
    return nets


def uplink_networks(cfg):
    """Подсети, до которых WAN-порт достаёт напрямую (домашняя сеть аплинка).

    Заданы вручную — берём их. Иначе выводим из адресов WAN-интерфейса.
    """
    configured = [n for n in (cfg.get("uplink_access") or {}).get("networks") or [] if n]
    if configured:
        result = []
        for item in configured:
            try:
                result.append(str(ipaddress.ip_network(item, strict=False)))
            except ValueError:
                log.warning("подсеть аплинка «%s» не разобралась", item)
        return result

    lan = ipaddress.ip_network(str(state.lan_network(cfg)))
    result = []
    for addr in netctl.iface_addresses(cfg["wan"]["interface"]):
        try:
            network = ipaddress.ip_interface(addr).network
        except ValueError:
            continue
        if network == lan or network.prefixlen >= 31:
            continue
        result.append(str(network))
    return result


def _bypass_elements(cfg):
    nets = list(BYPASS_NETS)
    _add_uncovered(nets, state.lan_network(cfg))
    # Сеть аплинка тоже мимо прокси: иначе NAS и принтер за WAN-портом
    # поехали бы через VPN-сервер и стали бы недоступны.
    for network in uplink_networks(cfg):
        _add_uncovered(nets, network)
    return ", ".join(nets)


PROTOCOLS = {"tcp": ["tcp"], "udp": ["udp"], "both": ["tcp", "udp"]}


def active_forwards(cfg):
    """Включённые правила проброса, прошедшие валидацию."""
    section = cfg.get("port_forward") or {}
    if not section.get("enabled", True):
        return []
    return [r for r in section.get("rules") or []
            if r.get("enabled", True) and not validate_forward(r, cfg)]


def validate_forward(rule, cfg):
    """Список претензий к одному правилу. Пустой список — правило годное."""
    problems = []
    name = rule.get("name") or "без имени"

    if rule.get("protocol", "tcp") not in PROTOCOLS:
        problems.append(f"«{name}»: протокол должен быть tcp, udp или both")

    for field, label in (("src_port", "внешний порт"), ("dest_port", "внутренний порт")):
        try:
            port = int(rule.get(field))
        except (TypeError, ValueError):
            problems.append(f"«{name}»: {label} — не число")
            continue
        if not 1 <= port <= 65535:
            problems.append(f"«{name}»: {label} вне диапазона 1–65535")

    lan = state.lan_network(cfg)
    try:
        dest = ipaddress.ip_address(rule.get("dest_ip", ""))
        if dest not in lan:
            problems.append(
                f"«{name}»: {dest} вне подсети LAN {lan}. Роутер пробрасывает только "
                f"в свою сеть — если устройство стоит за другим роутером, укажите "
                f"здесь его адрес в {lan}, а дальше настройте проброс уже на нём."
            )
    except ValueError:
        problems.append(f"«{name}»: некорректный адрес назначения")

    if rule.get("source"):
        try:
            ipaddress.ip_network(rule["source"], strict=False)
        except ValueError:
            problems.append(f"«{name}»: источник должен быть IP или подсетью")

    # Проброс порта управления увёл бы админку и SSH мимо роутера — на
    # headless-железке это билет в один конец.
    fw = cfg.get("firewall") or {}
    reserved = {22: "SSH", int(fw.get("admin_port") or settings.HTTP_PORT): "панель"}
    try:
        src_port = int(rule.get("src_port"))
    except (TypeError, ValueError):
        src_port = None
    if src_port in reserved:
        problems.append(
            f"«{name}»: порт {src_port} занят под {reserved[src_port]} самого роутера. "
            "Смените порт панели или используйте другой внешний порт."
        )
    return problems


def validate_forwards(cfg):
    problems = []
    seen = {}
    for rule in (cfg.get("port_forward") or {}).get("rules") or []:
        # Выключенное правило не должно мешать применению: недозаполненная
        # заготовка иначе намертво блокирует кнопку «Применить» целиком.
        if not rule.get("enabled", True):
            continue
        problems.extend(validate_forward(rule, cfg))
        try:
            # Панель шлёт порты строками, конфиг может хранить числами —
            # без приведения дубликат «443» и 443 проходит мимо проверки.
            port_key = int(rule.get("src_port"))
        except (TypeError, ValueError):
            continue
        for proto in PROTOCOLS.get(rule.get("protocol", "tcp"), []):
            key = (proto, port_key)
            if key in seen:
                problems.append(
                    f"«{rule.get('name') or 'без имени'}»: {proto}/{rule.get('src_port')} "
                    f"уже занят правилом «{seen[key]}»")
            seen[key] = rule.get("name") or "без имени"
    return problems


def _forward_nat_rules(cfg):
    """DNAT снаружи + hairpin изнутри для проброшенных портов."""
    wan_if = cfg["wan"]["interface"]
    lan_if = cfg["lan"]["interface"]
    lan_ip = cfg["lan"]["address"]
    lan_net = str(state.lan_network(cfg))
    prerouting, postrouting = [], []

    for rule in active_forwards(cfg):
        dest_ip = rule["dest_ip"]
        dest_port = int(rule["dest_port"])
        src_port = int(rule["src_port"])
        source = f"ip saddr {rule['source']} " if rule.get("source") else ""
        for proto in PROTOCOLS[rule.get("protocol", "tcp")]:
            prerouting.append(
                f'        iifname {{ "{wan_if}", "ppp*" }} {source}'
                f"{proto} dport {src_port} counter dnat to {dest_ip}:{dest_port}"
            )
            # Hairpin: из дома домен резолвится во внешний адрес роутера, и
            # пакет приходит на LAN-порт, а не на WAN — правило выше его не
            # видит. `fib daddr type local` ловит обращение к любому своему
            # адресу и переживает смену IP провайдером.
            prerouting.append(
                f'        iifname "{lan_if}" ip daddr != {lan_ip} '
                f"fib daddr type local {proto} dport {src_port} "
                f"counter dnat to {dest_ip}:{dest_port}"
            )
            # Обратный путь: без подмены источника ответ ушёл бы клиенту
            # напрямую от соседа по подсети, и тот бы его отбросил.
            postrouting.append(
                f"        ip saddr {lan_net} ip daddr {dest_ip} "
                f"{proto} dport {dest_port} counter masquerade"
            )
    return "\n".join(prerouting), "\n".join(postrouting)


def base_ruleset(cfg):
    """filter + nat: фаервол хоста и NAT для LAN. Не зависит от статуса VPN."""
    lan_if = cfg["lan"]["interface"]
    wan_if = cfg["wan"]["interface"]
    egress_pattern = state.wan_egress_pattern(cfg)
    lan_net = str(state.lan_network(cfg))
    fw = cfg["firewall"]

    wan_ports = []
    if fw.get("allow_wan_ssh", True):
        wan_ports.append(22)
    if fw.get("allow_wan_admin", True):
        wan_ports.append(int(fw.get("admin_port") or settings.HTTP_PORT))
    for p in fw.get("extra_wan_tcp_ports") or []:
        try:
            wan_ports.append(int(p))
        except (TypeError, ValueError):
            continue
    wan_ports = sorted(set(wan_ports))
    admin_port = int(fw.get("admin_port") or settings.HTTP_PORT)
    lan_ip = cfg["lan"]["address"]
    ports_rule = ""
    if wan_ports:
        joined = ", ".join(str(p) for p in wan_ports)
        ports_rule = f'        iifname {{ "{wan_if}", "ppp*" }} tcp dport {{ {joined} }} accept\n'

    uplink = cfg.get("uplink_access") or {}
    uplink_nets = uplink_networks(cfg)
    nets_set = "{ " + ", ".join(uplink_nets) + " }" if uplink_nets else ""

    # LAN -> сеть аплинка. По умолчанию разрешено и работает через NAT.
    lan_to_uplink_rule = ""
    if uplink_nets and not uplink.get("lan_to_uplink", True):
        lan_to_uplink_rule = (
            "        # Изоляция: клиентам LAN закрыт доступ в сеть аплинка.\n"
            f'        iifname "{lan_if}" ip daddr {nets_set} drop\n'
        )

    # Сеть аплинка -> LAN. Требует статического маршрута на роутере аплинка.
    if uplink.get("uplink_to_lan"):
        uplink_to_lan_rule = (
            "        # Хостам из сети аплинка разрешено стучаться в LAN.\n"
            f'        iifname {{ "{wan_if}", "ppp*" }} oifname "{lan_if}" accept\n'
        )
    else:
        uplink_to_lan_rule = (
            "        # Снаружи инициировать соединения внутрь LAN нельзя.\n"
            f'        iifname {{ "{wan_if}", "ppp*" }} oifname "{lan_if}" ct state new drop\n'
        )

    # Без маскарада хосты аплинка видят настоящие адреса клиентов LAN.
    no_nat_rule = ""
    if uplink_nets and uplink.get("no_nat_to_uplink"):
        no_nat_rule = (
            f"        # Трафик в сеть аплинка не маскарадим: на роутере аплинка\n"
            f"        # нужен маршрут {lan_net} через нас.\n"
            f'        oifname "{egress_pattern}" ip saddr {lan_net} ip daddr {nets_set} return\n'
        )

    dnat_rules, hairpin_rules = _forward_nat_rules(cfg)

    return f"""\
table inet {settings.NFT_FILTER}
delete table inet {settings.NFT_FILTER}
table ip {settings.NFT_NAT}
delete table ip {settings.NFT_NAT}

table inet {settings.NFT_FILTER} {{
    chain input {{
        # policy accept, а не drop: таблица живёт рядом с правилами Docker'а,
        # и промах в наших правилах не должен отрезать доступ к машине.
        # Всё нежелательное со стороны WAN гасится явным drop в конце цепочки.
        type filter hook input priority filter; policy accept;

        ct state established,related accept
        ct state invalid drop
        iif lo accept
        iifname "{lan_if}" accept
        meta l4proto {{ icmp, ipv6-icmp }} accept
{ports_rule}\
        # DHCP-клиент на WAN: ответ сервера приходит до появления conntrack.
        iifname {{ "{wan_if}", "ppp*" }} udp sport 67 udp dport 68 accept
        iifname {{ "{wan_if}", "ppp*" }} ct state new drop
    }}

    chain forward {{
        type filter hook forward priority filter; policy accept;

        ct state established,related accept
        ct state invalid drop
        # PPPoE отдаёт MTU 1492: без подгонки MSS часть сайтов виснет на
        # больших пакетах, потому что ICMP fragmentation needed режут провайдеры.
        oifname "ppp*" tcp flags syn tcp option maxseg size set rt mtu
{lan_to_uplink_rule}\
        iifname "{lan_if}" oifname {{ "{wan_if}", "ppp*" }} accept
        iifname "{lan_if}" oifname "{lan_if}" accept
        # Проброшенные порты: nat prerouting отрабатывает раньше forward,
        # поэтому такие пакеты уже помечены conntrack'ом как dnat.
        iifname {{ "{wan_if}", "ppp*" }} oifname "{lan_if}" ct status dnat accept
{uplink_to_lan_rule}\
    }}
}}

table ip {settings.NFT_NAT} {{
    chain prerouting {{
        type nat hook prerouting priority dstnat; policy accept;
        # Чтобы панель открывалась просто по адресу роутера, без :{admin_port}.
        # Строго на его собственный адрес — иначе увели бы весь HTTP из LAN.
        iifname "{lan_if}" ip daddr {lan_ip} tcp dport 80 redirect to :{admin_port}
{dnat_rules}
    }}

    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
{hairpin_rules}
{no_nat_rule}\
        oifname "{egress_pattern}" ip saddr {lan_net} masquerade
    }}
}}
"""


def tproxy_ruleset(cfg, enabled):
    """mangle: перехват LAN-трафика в TPROXY-инбаунд xray."""
    head = (
        f"table ip {settings.NFT_MANGLE}\n"
        f"delete table ip {settings.NFT_MANGLE}\n"
    )
    if not enabled:
        return head

    lan_if = cfg["lan"]["interface"]
    port = int(cfg["tproxy"]["port"])
    mark = int(cfg["tproxy"]["fwmark"])
    return head + f"""
table ip {settings.NFT_MANGLE} {{
    set bypass4 {{
        type ipv4_addr
        flags interval
        elements = {{ {_bypass_elements(cfg)} }}
    }}

    chain prerouting {{
        type filter hook prerouting priority mangle; policy accept;

        ip daddr @bypass4 accept
        # Обращения к собственным адресам роутера (включая внешний) в прокси не
        # заворачиваем: иначе hairpin-проброс уезжает в VPN вместо DNAT.
        fib daddr type local accept
        # Проброшенные порты мимо прокси — обязательно раньше tproxy-правил.
        # Ответ проброса идёт из LAN на публичный адрес клиента и выглядит как
        # обычный исходящий трафик: TPROXY утаскивал его в xray, соединение
        # рассыпалось, а инициатору прилетал RST.
        ct status dnat accept
        # Заворачиваем только транзит из LAN. Трафик, пришедший с WAN (в том
        # числе наш SSH), не трогаем вообще.
        iifname "{lan_if}" meta l4proto tcp tproxy to :{port} meta mark set {mark} accept
        iifname "{lan_if}" meta l4proto udp tproxy to :{port} meta mark set {mark} accept
    }}
}}
"""


def acct_ruleset(cfg):
    """Пер-клиентский учёт трафика через dynamic set со счётчиками."""
    lan_if = cfg["lan"]["interface"]
    return f"""\
table inet {settings.NFT_ACCT}
delete table inet {settings.NFT_ACCT}

table inet {settings.NFT_ACCT} {{
    set clients_up {{
        type ipv4_addr
        size 4096
        flags dynamic,timeout
        timeout 7d
        counter
    }}
    set clients_down {{
        type ipv4_addr
        size 4096
        flags dynamic,timeout
        timeout 7d
        counter
    }}

    chain accounting {{
        type filter hook forward priority -150; policy accept;
        iifname "{lan_if}" update @clients_up {{ ip saddr }}
        oifname "{lan_if}" update @clients_down {{ ip daddr }}
    }}
}}
"""


# ── сосуществование с Docker ─────────────────────────────────────────────────
#
# Docker держит собственную цепочку `ip filter FORWARD` с `policy drop`. nftables
# требует, чтобы пакет приняли ВСЕ базовые цепочки хука, поэтому наш `accept` в
# circle_filter ничего не решает — транзит всё равно дропается Docker'ом.
#
# Коварство в том, что при включённом VPN это незаметно: TPROXY доставляет
# пакеты локально в xray, минуя FORWARD, и интернет у клиентов работает. А вот
# трафик в обход прокси (сеть аплинка, да и весь LAN при выключенном VPN)
# уходит именно через FORWARD и умирает.
#
# Docker специально оставляет цепочку DOCKER-USER, которая вызывается первой и
# которую он не трогает. `accept` в ней завершает обход базовой цепочки, минуя
# policy drop.

DOCKER_USER_CHAIN = "DOCKER-USER"
DOCKER_RULE_COMMENT = "circle-router"


def docker_user_rules(cfg):
    lan_if = cfg["lan"]["interface"]
    wan_if = cfg["wan"]["interface"]
    uplink = cfg.get("uplink_access") or {}
    wan_set = f'{{ "{wan_if}", "ppp*" }}'

    rules = [
        f'iifname "{lan_if}" oifname {wan_set} counter accept',
        f'iifname "{lan_if}" oifname "{lan_if}" counter accept',
        f'iifname {wan_set} oifname "{lan_if}" ct state established,related counter accept',
        # Проброшенные порты тоже транзит, и policy drop у Docker'а убил бы их
        # первым же пакетом. Одно правило вместо перечисления всех прокидок.
        f'iifname {wan_set} oifname "{lan_if}" ct status dnat counter accept',
    ]
    if uplink.get("uplink_to_lan"):
        rules.append(f'iifname {wan_set} oifname "{lan_if}" counter accept')
    return rules


def _docker_user_handles():
    """Хендлы наших правил в DOCKER-USER (чужие не трогаем)."""
    raw = out(["nft", "-j", "-a", "list", "chain", "ip", "filter", DOCKER_USER_CHAIN])
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None
    handles = []
    for item in doc.get("nftables", []):
        rule = item.get("rule")
        if isinstance(rule, dict) and rule.get("comment") == DOCKER_RULE_COMMENT:
            handles.append(rule["handle"])
    return handles


def apply_docker_user(cfg):
    """Возвращает True, если цепочка есть и правила расставлены."""
    handles = _docker_user_handles()
    if handles is None:
        # Docker не управляет iptables (или образ другой) — значит, и его
        # policy drop нам не мешает.
        return False

    for handle in handles:
        run(["nft", "delete", "rule", "ip", "filter", DOCKER_USER_CHAIN,
             "handle", str(handle)], check=False)

    # insert кладёт правило в начало, поэтому идём с конца — так итоговый
    # порядок совпадёт с описанным в docker_user_rules().
    for rule in reversed(docker_user_rules(cfg)):
        run(["nft", "insert", "rule", "ip", "filter", DOCKER_USER_CHAIN,
             *rule.split(), "comment", DOCKER_RULE_COMMENT], check=False)
    return True


def docker_user_ok(cfg):
    """Быстрая проверка для watchdog: правила на месте и актуальны?"""
    handles = _docker_user_handles()
    if handles is None:
        return True
    return len(handles) == len(docker_user_rules(cfg))


def apply_base(cfg):
    _nft(base_ruleset(cfg))


def apply_tproxy(cfg, enabled):
    _nft(tproxy_ruleset(cfg, enabled))


def apply_accounting(cfg, force=False):
    """Пересоздаём таблицу учёта только при нужде — иначе обнулим счётчики."""
    if not force and table_exists("inet", settings.NFT_ACCT):
        chain = out(["nft", "list", "chain", "inet", settings.NFT_ACCT, "accounting"])
        if f'"{cfg["lan"]["interface"]}"' in chain:
            return False
    _nft(acct_ruleset(cfg))
    return True


def table_exists(family, name):
    return f"table {family} {name}" in out(["nft", "list", "tables"])


def tproxy_active():
    if not table_exists("ip", settings.NFT_MANGLE):
        return False
    chain = out(["nft", "list", "chain", "ip", settings.NFT_MANGLE, "prerouting"])
    return "tproxy" in chain


def client_counters():
    """{ip: {"up": bytes, "down": bytes}} из dynamic-сетов nftables."""
    result = {}
    for set_name, key in (("clients_up", "up"), ("clients_down", "down")):
        raw = out(["nft", "-j", "list", "set", "inet", settings.NFT_ACCT, set_name])
        if not raw:
            continue
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            continue
        for item in doc.get("nftables", []):
            for elem in (item.get("set") or {}).get("elem") or []:
                value = elem.get("elem") if isinstance(elem, dict) else None
                if not isinstance(value, dict):
                    continue
                addr = value.get("val")
                counter = value.get("counter") or {}
                if isinstance(addr, str):
                    result.setdefault(addr, {"up": 0, "down": 0})
                    result[addr][key] = int(counter.get("bytes", 0))
    return result


def teardown():
    """Полностью снять наши таблицы — для скрипта аварийного сброса."""
    script = "".join(
        f"table {fam} {name}\ndelete table {fam} {name}\n"
        for fam, name in (
            ("inet", settings.NFT_FILTER),
            ("ip", settings.NFT_NAT),
            ("ip", settings.NFT_MANGLE),
            ("inet", settings.NFT_ACCT),
        )
    )
    _nft(script)

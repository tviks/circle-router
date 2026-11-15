"""Перевод share-ссылок (vless/vmess/trojan/ss/wireguard) в xray-аутбаунды
и обратно."""

import base64
import ipaddress
import json
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

PROXY_PROTOCOLS = {"vless", "vmess", "trojan", "shadowsocks", "socks",
                   "http", "wireguard", "hysteria2"}


def _b64decode(blob):
    blob = blob.strip().replace("-", "+").replace("_", "/")
    blob += "=" * (-len(blob) % 4)
    return base64.b64decode(blob)


def stream_settings(*, network, security, host, path, sni, fingerprint,
                    pbk, sid, spx, flow, service_name="", header_type="",
                    alpn=""):
    s = {"network": network or "tcp", "security": security or "none"}

    if security == "tls":
        tls = {}
        if sni:
            tls["serverName"] = sni
        if fingerprint:
            tls["fingerprint"] = fingerprint
        if alpn:
            tls["alpn"] = [a for a in alpn.split(",") if a]
        if tls:
            s["tlsSettings"] = tls
    elif security == "reality":
        reality = {}
        if sni:
            reality["serverName"] = sni
        if fingerprint:
            reality["fingerprint"] = fingerprint
        if pbk:
            reality["publicKey"] = pbk
        if sid:
            reality["shortId"] = sid
        if spx:
            reality["spiderX"] = spx
        s["realitySettings"] = reality

    if network == "ws":
        ws = {"path": path or "/"}
        if host:
            ws["headers"] = {"Host": host}
        s["wsSettings"] = ws
    elif network in ("grpc", "gun"):
        s["network"] = "grpc"
        s["grpcSettings"] = {"serviceName": service_name or ""}
    elif network in ("h2", "http"):
        s["network"] = "http"
        http = {"path": path or "/"}
        if host:
            http["host"] = [h for h in host.split(",") if h]
        s["httpSettings"] = http
    elif network in ("xhttp", "splithttp"):
        s["network"] = network
        xh = {"path": path or "/"}
        if host:
            xh["host"] = host
        s[f"{network}Settings"] = xh
    elif network == "tcp":
        # HTTP-камуфляж включаем ТОЛЬКО при явном headerType=http.
        # Лишний tcpSettings.header.type ломает handshake Reality.
        if header_type == "http":
            tcp = {"header": {"type": "http"}}
            if host or path:
                tcp["header"]["request"] = {
                    "path": [path or "/"],
                    "headers": {"Host": [h for h in (host or "").split(",") if h]},
                }
            s["tcpSettings"] = tcp

    return s


# ── WireGuard ────────────────────────────────────────────────────────────────
#
# В xray-core WireGuard — userspace-протокол на обе стороны, отдельный бинарь
# не нужен. На роутере он живёт в контейнере, где нет /dev/net/tun, поэтому
# клиентский аутбаунд всегда собираем с noKernelTun: иначе ядро попытается
# создать TUN-интерфейс (capability NET_ADMIN у контейнера есть) и упадёт.

def _split_endpoint(value, default_port=51820):
    """'host:port', '[2001:db8::1]:51820' -> (host, port)."""
    value = (value or "").strip()
    if not value:
        return "", default_port
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest.lstrip(":")
        return host, int(port) if port.isdigit() else default_port
    host, sep, port = value.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return value, default_port


def _strip_prefix(value):
    return str(value).strip().split("/", 1)[0]


def _wg_reserved(value):
    if not value:
        return None
    parts = [p for p in str(value).replace(";", ",").replace(" ", ",").split(",") if p]
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    if len(numbers) != 3 or any(not 0 <= n <= 255 for n in numbers):
        return None
    return numbers


def _wg_dns(value):
    result = []
    for item in (value or "").replace(";", ",").split(","):
        item = _strip_prefix(item)
        if not item:
            continue
        try:
            ipaddress.ip_address(item)
        except ValueError:
            continue
        result.append(item)
    return result


def _wireguard_outbound(secret_key, address, peers, mtu=1420,
                        reserved=None, dns=None):
    if not secret_key:
        raise ValueError("в WireGuard-конфиге нет PrivateKey")
    if not address:
        address = ["10.0.0.2"]
    if not peers:
        raise ValueError("в WireGuard-конфиге нет ни одного [Peer]")
    for peer in peers:
        if not peer.get("publicKey"):
            raise ValueError("у пира WireGuard нет PublicKey")
        if not peer.get("endpoint"):
            raise ValueError("у пира WireGuard нет Endpoint — некуда подключаться")
        peer["allowedIPs"] = ([a.strip() for a in peer.get("allowedIPs") or [] if a.strip()]
                              or ["0.0.0.0/0", "::/0"])

    settings = {
        "secretKey": secret_key.strip(),
        "address": [_strip_prefix(a) for a in address if str(a).strip()],
        "peers": peers,
        # В Docker нет /dev/net/tun, а capability NET_ADMIN есть — автоопределение
        # ядра выбрало бы TUN и упало. gVisor-стек работает всегда.
        "noKernelTun": True,
        "mtu": int(mtu or 1420),
    }
    if reserved:
        settings["reserved"] = reserved
    if dns:
        settings["remoteDNS"] = dns
    return {"tag": "proxy", "protocol": "wireguard", "settings": settings}


def _parse_wireguard_link(url):
    """wireguard://<private>@host:port?publickey=…&address=…&mtu=…#name

    Формат не стандартизирован: панели кладут ключи и в userinfo, и в query.
    Принимаем оба варианта и разные написания имён параметров.
    """
    body, _, _fragment = url.split("://", 1)[1].partition("#")
    authority, _, query = body.partition("?")
    userinfo, at, hostpart = authority.rpartition("@")
    # Не parse_qs: он превращает '+' в пробел, а base64-ключи WireGuard
    # сплошь и рядом содержат '+' без percent-кодирования.
    qs = {}
    for pair in query.split("&"):
        key, _, value = pair.partition("=")
        if key:
            qs[key.lower()] = unquote(value)

    secret = unquote(userinfo) if at else ""
    secret = secret or qs.get("privatekey") or qs.get("secretkey") or qs.get("private")
    public = (qs.get("publickey") or qs.get("peerpublickey")
              or qs.get("pk") or qs.get("password"))
    if not public:
        raise ValueError("в wireguard-ссылке нет publickey")

    address = [a.strip() for a in (qs.get("address") or qs.get("ip")
                                    or qs.get("localaddress") or "").split(",") if a.strip()]
    allowed = [a.strip() for a in (qs.get("allowedips") or qs.get("allowed")
                                    or "0.0.0.0/0,::/0").split(",") if a.strip()]
    host, port = _split_endpoint(hostpart, 51820)
    if not host:
        raise ValueError("в wireguard-ссылке нет адреса сервера")

    peer = {"endpoint": f"[{host}]:{port}" if ":" in host else f"{host}:{port}",
            "publicKey": unquote(public), "allowedIPs": allowed}
    psk = qs.get("presharedkey") or qs.get("psk")
    if psk:
        peer["preSharedKey"] = unquote(psk)
    try:
        keepalive = int(qs.get("keepalive") or qs.get("persistentkeepalive") or 0)
    except ValueError:
        keepalive = 0
    if keepalive:
        peer["keepAlive"] = keepalive
    try:
        mtu = int(qs.get("mtu") or 1420)
    except ValueError:
        mtu = 1420
    return _wireguard_outbound(
        unquote(secret), address, [peer], mtu=mtu,
        reserved=_wg_reserved(qs.get("reserved")),
        dns=_wg_dns(qs.get("dns") or qs.get("remotedns")),
    )


def _parse_wireguard_config(text):
    """Клиентский wg-quick .conf: секции [Interface] и [Peer]."""
    interface, peers, current = {}, [], None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            if section == "interface":
                current = interface
            elif section == "peer":
                current = {}
                peers.append(current)
            else:
                current = None
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip().lower()] = value.strip()

    if not interface:
        raise ValueError("не нашёл секцию [Interface]")
    peer_list = []
    for item in peers:
        if not item.get("publickey"):
            continue
        endpoint = item.get("endpoint", "")
        host, port = _split_endpoint(endpoint, 51820)
        if not host:
            raise ValueError("в [Peer] нет Endpoint")
        allowed = [a.strip() for a in item.get("allowedips", "0.0.0.0/0,::/0").split(",")
                   if a.strip()]
        peer = {"endpoint": f"[{host}]:{port}" if ":" in host else f"{host}:{port}",
                "publicKey": item["publickey"], "allowedIPs": allowed}
        if item.get("presharedkey"):
            peer["preSharedKey"] = item["presharedkey"]
        try:
            keepalive = int(item.get("persistentkeepalive") or 0)
        except ValueError:
            keepalive = 0
        if keepalive:
            peer["keepAlive"] = keepalive
        peer_list.append(peer)

    address = [a.strip() for a in interface.get("address", "").split(",") if a.strip()]
    try:
        mtu = int(interface.get("mtu") or 1420)
    except ValueError:
        mtu = 1420
    return _wireguard_outbound(
        interface.get("privatekey", ""), address, peer_list, mtu=mtu,
        reserved=_wg_reserved(interface.get("reserved")),
        dns=_wg_dns(interface.get("dns")),
    )


def parse_url(url):
    """vless:// | vmess:// | trojan:// | ss:// | wireguard:// -> xray outbound."""
    url = (url or "").strip()
    if not url:
        return None

    if url.startswith(("wireguard://", "wg://")):
        return _parse_wireguard_link(url)
    if "[Interface]" in url and "[Peer]" in url:
        return _parse_wireguard_config(url)

    if url.startswith("vmess://"):
        data = json.loads(_b64decode(url[len("vmess://"):]).decode("utf-8"))
        return {
            "tag": "proxy",
            "protocol": "vmess",
            "settings": {"vnext": [{
                "address": data["add"],
                "port": int(data["port"]),
                "users": [{
                    "id": data["id"],
                    "alterId": int(data.get("aid", 0) or 0),
                    "security": data.get("scy", "auto"),
                }],
            }]},
            "streamSettings": stream_settings(
                network=data.get("net", "tcp"),
                security=(data.get("tls") or "none") or "none",
                host=data.get("host", ""),
                path=data.get("path", "/"),
                sni=data.get("sni", ""),
                fingerprint=data.get("fp", ""),
                pbk="", sid="", spx="", flow="",
                service_name=data.get("path", "") if data.get("net") == "grpc" else "",
                header_type=data.get("type", ""),
            ),
        }

    parsed = urlparse(url)
    qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
    scheme = parsed.scheme

    if scheme == "vless":
        return {
            "tag": "proxy",
            "protocol": "vless",
            "settings": {"vnext": [{
                "address": parsed.hostname,
                "port": parsed.port or 443,
                "users": [{
                    "id": unquote(parsed.username or ""),
                    "encryption": qs.get("encryption", "none") or "none",
                    "flow": qs.get("flow", ""),
                }],
            }]},
            "streamSettings": stream_settings(
                network=qs.get("type", "tcp"),
                security=qs.get("security", "none"),
                host=unquote(qs.get("host", "")),
                path=unquote(qs.get("path", "/")),
                sni=qs.get("sni", ""),
                fingerprint=qs.get("fp", ""),
                pbk=qs.get("pbk", ""),
                sid=qs.get("sid", ""),
                spx=unquote(qs.get("spx", "")),
                flow=qs.get("flow", ""),
                service_name=unquote(qs.get("serviceName", "")),
                header_type=qs.get("headerType", ""),
                alpn=unquote(qs.get("alpn", "")),
            ),
        }

    if scheme == "trojan":
        return {
            "tag": "proxy",
            "protocol": "trojan",
            "settings": {"servers": [{
                "address": parsed.hostname,
                "port": parsed.port or 443,
                "password": unquote(parsed.username or ""),
            }]},
            "streamSettings": stream_settings(
                network=qs.get("type", "tcp"),
                security=qs.get("security", "tls"),
                host=unquote(qs.get("host", "")),
                path=unquote(qs.get("path", "/")),
                sni=qs.get("sni", ""),
                fingerprint=qs.get("fp", ""),
                pbk="", sid="", spx="", flow="",
                service_name=unquote(qs.get("serviceName", "")),
                header_type=qs.get("headerType", ""),
                alpn=unquote(qs.get("alpn", "")),
            ),
        }

    if scheme == "ss":
        userinfo = parsed.username or ""
        decoded = unquote(userinfo)
        if ":" not in decoded:
            decoded = _b64decode(userinfo).decode("utf-8")
        method, _, password = decoded.partition(":")
        return {
            "tag": "proxy",
            "protocol": "shadowsocks",
            "settings": {"servers": [{
                "address": parsed.hostname,
                "port": parsed.port or 8388,
                "method": method,
                "password": password,
            }]},
        }

    raise ValueError(f"неподдерживаемая схема ссылки: {scheme!r}")


def normalize_outbound(outbound):
    """Санитайз готового аутбаунда из Xray-JSON подписки.

    Обязательно срезаем `sockopt.mark`: помеченный исходящий сокет xray попадёт
    обратно в наше policy-правило (fwmark -> table 100 -> dev lo) и зациклится.
    """
    settings = json.loads(json.dumps(outbound.get("settings", {}) or {}))
    if outbound.get("protocol") == "wireguard" and isinstance(settings, dict):
        # Контейнер без /dev/net/tun: ядро всё равно попытается создать TUN,
        # потому что NET_ADMIN есть. Принудительно оставляем gVisor-стек.
        settings.setdefault("noKernelTun", True)
    result = {
        "tag": "proxy",
        "protocol": outbound.get("protocol"),
        "settings": settings,
    }
    stream = outbound.get("streamSettings")
    if isinstance(stream, dict):
        stream = json.loads(json.dumps(stream))
        sockopt = stream.get("sockopt")
        if isinstance(sockopt, dict):
            sockopt.pop("mark", None)
            if not sockopt:
                stream.pop("sockopt", None)
        result["streamSettings"] = stream
    if isinstance(outbound.get("mux"), dict):
        result["mux"] = outbound["mux"]
    return result


def _stream_query(stream):
    q = {}
    network = stream.get("network", "tcp")
    security = stream.get("security", "none")
    q["type"] = network
    q["security"] = security

    if security == "tls":
        tls = stream.get("tlsSettings") or {}
        if tls.get("serverName"):
            q["sni"] = tls["serverName"]
        if tls.get("fingerprint"):
            q["fp"] = tls["fingerprint"]
        if tls.get("alpn"):
            q["alpn"] = ",".join(tls["alpn"])
    elif security == "reality":
        r = stream.get("realitySettings") or {}
        for src, dst in (("serverName", "sni"), ("fingerprint", "fp"),
                         ("publicKey", "pbk"), ("shortId", "sid"),
                         ("spiderX", "spx")):
            if r.get(src):
                q[dst] = r[src]

    if network == "ws":
        ws = stream.get("wsSettings") or {}
        if ws.get("path"):
            q["path"] = ws["path"]
        host = (ws.get("headers") or {}).get("Host")
        if host:
            q["host"] = host
    elif network == "grpc":
        gs = stream.get("grpcSettings") or {}
        if gs.get("serviceName"):
            q["serviceName"] = gs["serviceName"]
    elif network == "tcp":
        header = ((stream.get("tcpSettings") or {}).get("header") or {})
        if header.get("type") == "http":
            q["headerType"] = "http"

    return q


def outbound_to_url(outbound, name=""):
    """Обратное преобразование, чтобы импортированный сервер можно было править."""
    proto = outbound.get("protocol")
    settings_ = outbound.get("settings") or {}
    stream = outbound.get("streamSettings") or {}
    frag = "#" + quote(name) if name else ""

    if proto == "vless":
        v = (settings_.get("vnext") or [{}])[0]
        u = (v.get("users") or [{}])[0]
        q = _stream_query(stream)
        if u.get("encryption") and u["encryption"] != "none":
            q["encryption"] = u["encryption"]
        if u.get("flow"):
            q["flow"] = u["flow"]
        return f"vless://{u.get('id', '')}@{v.get('address', '')}:{v.get('port', 443)}?{urlencode(q)}{frag}"

    if proto == "vmess":
        v = (settings_.get("vnext") or [{}])[0]
        u = (v.get("users") or [{}])[0]
        network = stream.get("network", "tcp")
        vm = {
            "v": "2", "ps": name,
            "add": v.get("address", ""), "port": str(v.get("port", 443)),
            "id": u.get("id", ""), "aid": str(u.get("alterId", 0)),
            "scy": u.get("security", "auto"), "net": network, "type": "none",
            "host": "", "path": "",
            "tls": stream.get("security", "") if stream.get("security") != "none" else "",
            "sni": (stream.get("tlsSettings") or {}).get("serverName", ""),
        }
        if network == "ws":
            ws = stream.get("wsSettings") or {}
            vm["path"] = ws.get("path", "")
            vm["host"] = (ws.get("headers") or {}).get("Host", "")
        elif network == "grpc":
            vm["path"] = (stream.get("grpcSettings") or {}).get("serviceName", "")
        blob = base64.b64encode(json.dumps(vm, ensure_ascii=False).encode()).decode()
        return "vmess://" + blob

    if proto == "trojan":
        srv = (settings_.get("servers") or [{}])[0]
        q = _stream_query(stream)
        pwd = quote(srv.get("password", ""), safe="")
        return f"trojan://{pwd}@{srv.get('address', '')}:{srv.get('port', 443)}?{urlencode(q)}{frag}"

    if proto == "shadowsocks":
        srv = (settings_.get("servers") or [{}])[0]
        userinfo = f"{srv.get('method', '')}:{srv.get('password', '')}"
        blob = base64.urlsafe_b64encode(userinfo.encode()).decode().rstrip("=")
        return f"ss://{blob}@{srv.get('address', '')}:{srv.get('port', 8388)}{frag}"

    raise ValueError(f"протокол {proto!r} нельзя выразить ссылкой")


def summarize(outbound):
    """Человекочитаемая выжимка для карточки сервера в UI."""
    if not outbound:
        return {}
    proto = outbound.get("protocol")
    settings_ = outbound.get("settings") or {}
    stream = outbound.get("streamSettings") or {}
    s = {"protocol": proto}

    if proto in ("vless", "vmess"):
        v = (settings_.get("vnext") or [{}])[0]
        u = (v.get("users") or [{}])[0]
        s.update({
            "address": v.get("address"), "port": v.get("port"),
            "id": u.get("id"), "encryption": u.get("encryption") or u.get("security"),
            "flow": u.get("flow"),
        })
    elif proto == "trojan":
        srv = (settings_.get("servers") or [{}])[0]
        s.update({"address": srv.get("address"), "port": srv.get("port")})
    elif proto == "shadowsocks":
        srv = (settings_.get("servers") or [{}])[0]
        s.update({"address": srv.get("address"), "port": srv.get("port"),
                  "method": srv.get("method")})
    elif proto == "hysteria":
        s.update({"address": settings_.get("address"), "port": settings_.get("port")})
    elif proto == "wireguard":
        peer = (settings_.get("peers") or [{}])[0]
        host, port = _split_endpoint(peer.get("endpoint"))
        s.update({"address": host, "port": port,
                  "allowed_ips": ", ".join(peer.get("allowedIPs") or [])})

    if proto == "wireguard":
        s["network"] = "udp"
        s["security"] = "udp"
    else:
        s["network"] = stream.get("network", "tcp")
        s["security"] = stream.get("security", "none")
    if (stream.get("sockopt") or {}).get("dialerProxy"):
        s["fragment"] = "tlshello"
    if stream.get("realitySettings"):
        r = stream["realitySettings"]
        pbk = r.get("publicKey") or ""
        s.update({
            "sni": r.get("serverName"), "fingerprint": r.get("fingerprint"),
            "public_key": (pbk[:16] + "…") if pbk else "",
            "short_id": r.get("shortId"),
        })
    elif stream.get("tlsSettings"):
        t = stream["tlsSettings"]
        s.update({"sni": t.get("serverName"), "fingerprint": t.get("fingerprint")})
    return s


def endpoint(outbound):
    """(host, port) аутбаунда — нужен для замера задержки до сервера."""
    settings_ = outbound.get("settings") or {}
    if outbound.get("protocol") == "wireguard":
        peer = (settings_.get("peers") or [{}])[0]
        return _split_endpoint(peer.get("endpoint"), 51820)
    if outbound.get("protocol") == "hysteria":
        return settings_.get("address"), int(settings_.get("port") or 443)
    if outbound.get("protocol") in ("vless", "vmess"):
        v = (settings_.get("vnext") or [{}])[0]
        return v.get("address"), int(v.get("port") or 443)
    srv = (settings_.get("servers") or [{}])[0]
    return srv.get("address"), int(srv.get("port") or 443)

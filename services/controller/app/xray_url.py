"""Перевод share-ссылок (vless/vmess/trojan/ss) в xray-аутбаунды и обратно."""

import base64
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


def parse_url(url):
    """vless:// | vmess:// | trojan:// | ss:// -> xray outbound (tag=proxy)."""
    url = (url or "").strip()
    if not url:
        return None

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
    result = {
        "tag": "proxy",
        "protocol": outbound.get("protocol"),
        "settings": outbound.get("settings", {}) or {},
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

    s["network"] = stream.get("network", "tcp")
    s["security"] = stream.get("security", "none")
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
    if outbound.get("protocol") in ("vless", "vmess"):
        v = (settings_.get("vnext") or [{}])[0]
        return v.get("address"), int(v.get("port") or 443)
    srv = (settings_.get("servers") or [{}])[0]
    return srv.get("address"), int(srv.get("port") or 443)

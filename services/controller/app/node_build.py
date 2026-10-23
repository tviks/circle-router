"""Конфиги своей VDS-ноды: секреты, кандидаты протоколов, серверные инбаунды
и парные им клиентские аутбаунды для роутера.

Кандидат — это связка «что слушает сервер» (транспорт, порт, маскировочный
SNI) и «как к нему стучится роутер» (uTLS-отпечаток, фрагментация
ClientHello). Подбор перебирает кандидатов и оставляет на сервере ровно один
инбаунд — тот, что лучше всего пробивается через текущий аплинк.
"""

import base64
import datetime
import hashlib
import secrets
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.x509.oid import NameOID

# Пути на сервере. Только бинарь ядра, конфиг и unit — никакой панели.
REMOTE_DIR = "/usr/local/lib/circle-node"
REMOTE_BIN = f"{REMOTE_DIR}/xray"
REMOTE_ETC = "/usr/local/etc/circle-node"
REMOTE_CONFIG = f"{REMOTE_ETC}/config.json"
REMOTE_PROBE = f"{REMOTE_ETC}/probe.json"
REMOTE_CERT = f"{REMOTE_ETC}/hy2.crt"
REMOTE_KEY = f"{REMOTE_ETC}/hy2.key"
SERVICE = "circle-node"
PROBE_SERVICE = "circle-node-probe"
SERVICE_USER = "circle-node"

# Маскировочные домены для Reality: TLS 1.3 + HTTP/2, без редиректа на чужой
# домен, не мегабренды вроде apple/icloud (ядро на них ругается, и такие SNI с
# дешёвых VPS банят первыми). Перебираются по очереди, лучшие проверяются
# вживую с роутера.
DEFAULT_SNI = [
    "www.microsoft.com",
    "dl.google.com",
    "addons.mozilla.org",
    "www.samsung.com",
    "www.nvidia.com",
    "github.com",
]

# Только детерминированные отпечатки: `randomized` с Reality проходит через
# раз (случайный ClientHello бывает несовместим), `android` не проходит вовсе
# — проверено на xray 26.3.27. Боевому профилю нужна воспроизводимость.
FINGERPRINTS = ["chrome", "firefox", "safari"]

# Приватные сети на стороне сервера закрыты: иначе через туннель можно было бы
# достучаться до сервисов самой VDS и её соседей по дата-центру.
PRIVATE_CIDRS = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4",
    "240.0.0.0/4", "::1/128", "fc00::/7", "fe80::/10",
]

# Порядок портов — порядок предпочтения. Reality лучше всего живёт на 443:
# на других портах ядро само предупреждает, что это ускоряет бан IP.
TRANSPORTS = {
    "vision": {"label": "VLESS · Reality · Vision", "network": "tcp",
               "ports": [443, 8443, 2083, 2087], "sni": True, "tcp": True},
    "xhttp": {"label": "VLESS · Reality · XHTTP", "network": "tcp",
              "ports": [8443, 443, 2096, 2053], "sni": True, "tcp": True},
    "grpc": {"label": "VLESS · Reality · gRPC", "network": "tcp",
             "ports": [2053, 2083, 443, 8443], "sni": True, "tcp": True},
    "ss2022": {"label": "Shadowsocks-2022", "network": "tcp",
               "ports": [], "sni": False, "tcp": True, "fixed_client": True},
    "hy2": {"label": "Hysteria2 · QUIC", "network": "udp",
            "ports": [443, 8443], "sni": False, "tcp": False, "fixed_client": True,
            "experimental": True},
}

FRAGMENT = {"packets": "tlshello", "length": "100-200", "interval": "10-20"}


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _self_signed(name):
    """Сертификат для Hysteria2. Клиент пиннит его по SHA-256, поэтому
    удостоверяющий центр не нужен."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
            .sign(key, hashes.SHA256()))
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(serialization.Encoding.PEM,
                                serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    digest = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    return cert_pem, key_pem, digest


def new_secrets():
    private = X25519PrivateKey.generate()
    raw_priv = private.private_bytes(serialization.Encoding.Raw,
                                     serialization.PrivateFormat.Raw,
                                     serialization.NoEncryption())
    raw_pub = private.public_key().public_bytes(serialization.Encoding.Raw,
                                                serialization.PublicFormat.Raw)
    hy2_name = DEFAULT_SNI[0]
    cert_pem, key_pem, digest = _self_signed(hy2_name)
    return {
        "uuid": str(uuid.uuid4()),
        "reality_private": _b64url(raw_priv),
        "reality_public": _b64url(raw_pub),
        "short_id": secrets.token_hex(8),
        "xhttp_path": "/" + secrets.token_hex(6),
        "grpc_service": secrets.token_hex(6),
        # 2022-blake3-aes-128-gcm требует ключ ровно 16 байт в base64.
        "ss_password": base64.b64encode(secrets.token_bytes(16)).decode(),
        "ss_port": 20000 + secrets.randbelow(30000),
        "hy2_auth": secrets.token_urlsafe(18),
        "hy2_sni": hy2_name,
        "hy2_cert": cert_pem,
        "hy2_key": key_pem,
        "hy2_cert_sha256": digest,
    }


# ── кандидаты ────────────────────────────────────────────────────────────────

def cand_key(c):
    return (f"{c['transport']}:{c['port']}:{c.get('sni') or '-'}:"
            f"{c.get('fp') or '-'}:{'frag' if c.get('frag') else 'plain'}")


def spec_key(c):
    """Серверная часть кандидата: что именно слушает нода."""
    return f"{c['transport']}:{c['port']}:{c.get('sni') or '-'}"


def network_of(c):
    return TRANSPORTS[c["transport"]]["network"]


def describe(c):
    meta = TRANSPORTS[c["transport"]]
    parts = [meta["label"], f":{c['port']}"]
    if c.get("sni"):
        parts.append(c["sni"])
    if not meta.get("fixed_client"):
        parts.append(f"fp {c.get('fp') or 'chrome'}")
        if c.get("frag"):
            parts.append("фрагментация")
    return " · ".join(parts)


def default_ports(node, transport):
    if transport == "ss2022":
        return [int(node["secrets"]["ss_port"])]
    return list(TRANSPORTS[transport]["ports"])


def pick_port(node, transport, busy):
    """Первый свободный порт из списка предпочтений транспорта. busy —
    множество (network, port), уже занятых другими инбаундами."""
    network = TRANSPORTS[transport]["network"]
    for port in default_ports(node, transport):
        if (network, port) not in busy:
            return port
    return None


# ── сервер ───────────────────────────────────────────────────────────────────

def _reality(node, sni):
    s = node["secrets"]
    return {
        "show": False,
        "dest": f"{sni}:443",
        "xver": 0,
        "serverNames": [sni],
        "privateKey": s["reality_private"],
        "shortIds": [s["short_id"]],
    }


def server_inbound(node, c, tag):
    s = node["secrets"]
    t = c["transport"]
    port = int(c["port"])
    if t == "vision":
        return {
            "tag": tag, "port": port, "protocol": "vless",
            "settings": {"clients": [{"id": s["uuid"], "flow": "xtls-rprx-vision"}],
                         "decryption": "none"},
            "streamSettings": {"network": "tcp", "security": "reality",
                               "realitySettings": _reality(node, c["sni"])},
        }
    if t == "xhttp":
        return {
            "tag": tag, "port": port, "protocol": "vless",
            "settings": {"clients": [{"id": s["uuid"]}], "decryption": "none"},
            "streamSettings": {"network": "xhttp", "security": "reality",
                               "xhttpSettings": {"path": s["xhttp_path"], "mode": "auto"},
                               "realitySettings": _reality(node, c["sni"])},
        }
    if t == "grpc":
        return {
            "tag": tag, "port": port, "protocol": "vless",
            "settings": {"clients": [{"id": s["uuid"]}], "decryption": "none"},
            "streamSettings": {"network": "grpc", "security": "reality",
                               "grpcSettings": {"serviceName": s["grpc_service"]},
                               "realitySettings": _reality(node, c["sni"])},
        }
    if t == "ss2022":
        return {
            "tag": tag, "port": port, "protocol": "shadowsocks",
            "settings": {"method": "2022-blake3-aes-128-gcm",
                         "password": s["ss_password"], "network": "tcp,udp"},
        }
    if t == "hy2":
        return {
            "tag": tag, "port": port, "protocol": "hysteria",
            "settings": {"version": 2, "clients": [{"auth": s["hy2_auth"], "email": "router"}]},
            "streamSettings": {
                "network": "hysteria", "security": "tls",
                "tlsSettings": {"alpn": ["h3"], "certificates": [
                    {"certificateFile": REMOTE_CERT, "keyFile": REMOTE_KEY}]},
                "hysteriaSettings": {"version": 2},
            },
        }
    raise ValueError(f"неизвестный транспорт {t}")


def server_config(node, specs):
    """Конфиг ядра на ноде. Минимум движущихся частей: без API, статистики,
    сниффинга, geo-баз и access-лога — только инбаунды и выход в интернет."""
    return {
        "log": {"loglevel": "warning", "access": "none", "dnsLog": False},
        "inbounds": [server_inbound(node, c, f"in-{i}") for i, c in enumerate(specs)],
        "outbounds": [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {"domainStrategy": "AsIs", "rules": [
            {"type": "field", "ip": PRIVATE_CIDRS, "outboundTag": "block"},
        ]},
        "policy": {"levels": {"0": {"handshake": 4, "connIdle": 300,
                                    "uplinkOnly": 1, "downlinkOnly": 1}}},
    }


def uses_hy2(specs):
    return any(c["transport"] == "hy2" for c in specs)


# ── клиент (роутер) ──────────────────────────────────────────────────────────

def _client_reality(node, c):
    return {
        "serverName": c["sni"],
        "fingerprint": c.get("fp") or "chrome",
        "publicKey": node["secrets"]["reality_public"],
        "shortId": node["secrets"]["short_id"],
        "spiderX": "/",
    }


def client_outbounds(node, c, tag="proxy"):
    """(основной аутбаунд, [вспомогательные]) для роутера.

    Фрагментация ClientHello делается отдельным freedom-аутбаундом, через
    который основной устанавливает TCP (sockopt.dialerProxy). На сервере это
    просто TCP-поток, порезанный на мелкие сегменты, — ничего настраивать не
    нужно, а DPI, собирающий SNI из первого пакета, остаётся ни с чем.
    """
    s = node["secrets"]
    t = c["transport"]
    host = node["host"]
    port = int(c["port"])
    extras = []

    if t in ("vision", "xhttp", "grpc"):
        user = {"id": s["uuid"], "encryption": "none"}
        stream = {"security": "reality", "realitySettings": _client_reality(node, c)}
        if t == "vision":
            user["flow"] = "xtls-rprx-vision"
            stream["network"] = "tcp"
        elif t == "xhttp":
            stream["network"] = "xhttp"
            stream["xhttpSettings"] = {"path": s["xhttp_path"], "mode": "auto"}
        else:
            stream["network"] = "grpc"
            stream["grpcSettings"] = {"serviceName": s["grpc_service"]}
        outbound = {"tag": tag, "protocol": "vless",
                    "settings": {"vnext": [{"address": host, "port": port, "users": [user]}]},
                    "streamSettings": stream}
    elif t == "ss2022":
        outbound = {"tag": tag, "protocol": "shadowsocks",
                    "settings": {"servers": [{"address": host, "port": port,
                                              "method": "2022-blake3-aes-128-gcm",
                                              "password": s["ss_password"]}]}}
    elif t == "hy2":
        outbound = {"tag": tag, "protocol": "hysteria",
                    "settings": {"version": 2, "address": host, "port": port},
                    "streamSettings": {
                        "network": "hysteria", "security": "tls",
                        "tlsSettings": {"serverName": s["hy2_sni"], "alpn": ["h3"],
                                        "pinnedPeerCertSha256": s["hy2_cert_sha256"]},
                        "hysteriaSettings": {"version": 2, "auth": s["hy2_auth"]},
                    }}
    else:
        raise ValueError(f"неизвестный транспорт {t}")

    if c.get("frag") and TRANSPORTS[t]["tcp"]:
        frag_tag = f"{tag}-frag"
        outbound.setdefault("streamSettings", {}).setdefault("sockopt", {})["dialerProxy"] = frag_tag
        extras.append({"tag": frag_tag, "protocol": "freedom",
                       "settings": {"fragment": dict(FRAGMENT)}})
    return outbound, extras


def retag(outbound, extras, tag):
    """Переименовать пару аутбаундов, чтобы в одном xray жило много кандидатов."""
    import json
    outbound = json.loads(json.dumps(outbound))
    extras = json.loads(json.dumps(extras))
    mapping = {e["tag"]: f"{tag}-x{i}" for i, e in enumerate(extras)}
    outbound["tag"] = tag
    sockopt = (outbound.get("streamSettings") or {}).get("sockopt") or {}
    if sockopt.get("dialerProxy") in mapping:
        sockopt["dialerProxy"] = mapping[sockopt["dialerProxy"]]
    for e in extras:
        e["tag"] = mapping[e["tag"]]
    return outbound, extras

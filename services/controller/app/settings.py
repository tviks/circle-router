"""Пути, порты и прочие константы, которые задаются окружением контейнера."""

import os
from pathlib import Path

DATA = Path(os.environ.get("CR_DATA", "/data"))

CONFIG_PATH = DATA / "config" / "router.json"
XRAY_CONFIG_PATH = DATA / "xray" / "config.json"
DNSMASQ_CONFIG_PATH = DATA / "dnsmasq" / "dnsmasq.conf"
DNSMASQ_LEASES = DATA / "dnsmasq" / "dnsmasq.leases"
# Тот же файл, но глазами контейнера dnsmasq: у него ./data/dnsmasq
# смонтирован в /etc/dnsmasq, а не в /data.
DNSMASQ_LEASES_IN_CONTAINER = "/etc/dnsmasq/dnsmasq.leases"
DNSMASQ_HOSTS = DATA / "dnsmasq" / "hosts"
BLOCKLIST_PATH = DATA / "dnsmasq" / "blocklist.conf"
# Тот же файл глазами контейнера dnsmasq.
BLOCKLIST_IN_CONTAINER = "/etc/dnsmasq/blocklist.conf"
BLOCKLIST_META = DATA / "config" / "adblock.json"
# Хеш пароля живёт здесь, а не только в .env: смена пароля из панели
# не должна требовать перезапуска контейнера.
AUTH_STORE = DATA / "config" / "auth.json"
GEO_DIR = DATA / "geo"
GEOSITE_DAT = GEO_DIR / "geosite.dat"
GEOIP_DAT = GEO_DIR / "geoip.dat"
DB_PATH = DATA / "db" / "metrics.sqlite3"
PPP_DIR = DATA / "ppp"
CACHE_DIR = DATA / "cache"

# Каталог systemd-networkd хоста. Туда кладётся файл, помечающий LAN-интерфейс
# как unmanaged, иначе networkd/netplan сносит наш статический адрес.
HOST_NETWORKD_DIR = Path("/host/systemd-network")

HTTP_PORT = int(os.environ.get("CR_HTTP_PORT", "8088"))
ADMIN_USER = os.environ.get("CR_ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("CR_ADMIN_PASSWORD_HASH", "")
SECRET_KEY = os.environ.get("CR_SECRET_KEY", "")
SESSION_COOKIE = "circle_session"
SESSION_TTL = 14 * 24 * 3600

XRAY_CONTAINER = os.environ.get("CR_XRAY_CONTAINER", "circle-xray")
DNSMASQ_CONTAINER = os.environ.get("CR_DNSMASQ_CONTAINER", "circle-dnsmasq")
DOCKER_SOCKET = os.environ.get("CR_DOCKER_SOCKET", "/var/run/docker.sock")

XRAY_BIN = os.environ.get("CR_XRAY_BIN", "/usr/local/bin/xray")
XRAY_API_ADDR = "127.0.0.1:10085"

# Имена наших nftables-таблиц. Отдельные таблицы (а не flush ruleset) — чтобы
# не снести правила Docker'а и не остаться без сети на самом хосте.
NFT_FILTER = "circle_filter"
NFT_NAT = "circle_nat"
NFT_MANGLE = "circle_mangle"
NFT_ACCT = "circle_acct"

DEFAULTS = {
    "wan_if": os.environ.get("CR_WAN_IF", "enp2s0"),
    "lan_if": os.environ.get("CR_LAN_IF", "enp1s0"),
    "lan_address": os.environ.get("CR_LAN_ADDRESS", "192.168.10.1"),
    "lan_prefix": int(os.environ.get("CR_LAN_PREFIX", "24")),
}

for _d in (CONFIG_PATH.parent, XRAY_CONFIG_PATH.parent, DNSMASQ_CONFIG_PATH.parent,
           GEO_DIR, DB_PATH.parent, PPP_DIR, CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

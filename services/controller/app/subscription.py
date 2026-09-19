"""Импорт серверов из подписки: Base64-список ссылок или Xray-JSON."""

import base64
import binascii
import json
import logging
from urllib.parse import unquote, urlparse

import httpx

from . import state, xray_url

log = logging.getLogger("circle.sub")

# Панели вроде Remnawave/Marzban отдают разный формат в зависимости от
# User-Agent: под «браузером» прилетает HTML, под клиентом — сама подписка.
DEFAULT_UA = "v2rayNG/1.9.5"
SCHEMES = ("vless://", "vmess://", "trojan://", "ss://")


def fetch(url, user_agent=DEFAULT_UA, timeout=25):
    headers = {"User-Agent": user_agent or DEFAULT_UA, "Accept": "*/*"}
    with httpx.Client(follow_redirects=True, timeout=timeout) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.text


def _maybe_b64(text):
    stripped = "".join(text.split())
    if not stripped or any(stripped.startswith(s) for s in SCHEMES):
        return text
    try:
        padded = stripped.replace("-", "+").replace("_", "/")
        padded += "=" * (-len(padded) % 4)
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return text
    return decoded if any(s in decoded for s in SCHEMES) else text


def _name_from_url(url, index):
    fragment = urlparse(url).fragment
    if fragment:
        return unquote(fragment)[:80]
    return f"Сервер {index}"


def _parse_links(text):
    servers = []
    for line in text.splitlines():
        line = line.strip()
        if not line or not any(line.startswith(s) for s in SCHEMES):
            continue
        try:
            xray_url.parse_url(line)
        except Exception as exc:                       # noqa: BLE001
            log.info("пропускаю нераспознанную ссылку: %s", exc)
            continue
        servers.append({
            "id": state.new_id(),
            "name": _name_from_url(line, len(servers) + 1),
            "url": line,
        })
    return servers


def _extract_proxy_outbound(entry):
    outbounds = entry.get("outbounds") or []
    for o in outbounds:
        if o.get("tag") == "proxy":
            return o
    for o in outbounds:
        if o.get("protocol") in xray_url.PROXY_PROTOCOLS:
            return o
    return None


def _parse_xray_json(text):
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return None
    entries = doc if isinstance(doc, list) else [doc]
    servers = []
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        outbound = _extract_proxy_outbound(entry)
        if not outbound:
            continue
        name = entry.get("remarks") or entry.get("name") or f"Сервер {index}"
        normalized = xray_url.normalize_outbound(outbound)
        server = {"id": state.new_id(), "name": str(name)[:80], "url": ""}
        try:
            # Если удаётся выразить ссылкой — так удобнее: её можно править в UI.
            server["url"] = xray_url.outbound_to_url(normalized, name=str(name))
        except (ValueError, KeyError, IndexError):
            server["outbound"] = normalized
        servers.append(server)
    return servers or None


def parse(text):
    servers = _parse_xray_json(text)
    if servers:
        return servers
    return _parse_links(_maybe_b64(text))


def import_subscription(url, user_agent=DEFAULT_UA):
    text = fetch(url, user_agent=user_agent)
    servers = parse(text)
    if not servers:
        raise ValueError("в подписке не нашлось ни одного распознанного сервера")
    return servers

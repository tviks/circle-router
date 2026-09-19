"""Разбор geosite.dat/geoip.dat: список категорий для подсказок в UI."""

import json
import logging
import time

import httpx

from . import settings

log = logging.getLogger("circle.geo")

GEOSITE_URL = "https://github.com/v2fly/domain-list-community/releases/latest/download/dlc.dat"
GEOIP_URL = "https://github.com/v2fly/geoip/releases/latest/download/geoip.dat"

_CACHE = settings.CACHE_DIR / "geo_categories.json"


def _read_varint(data, i):
    shift = 0
    result = 0
    while i < len(data):
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            break
    return None, i


def _parse_categories(path):
    """`repeated GeoSite list = 1` — вытаскиваем только country_code каждой записи."""
    try:
        data = path.read_bytes()
    except OSError:
        return []

    categories = []
    i, n = 0, len(data)
    while i < n:
        tag, i = _read_varint(data, i)
        if tag is None:
            break
        wire, field = tag & 7, tag >> 3
        if field == 1 and wire == 2:
            length, i = _read_varint(data, i)
            if length is None:
                break
            entry, i = data[i:i + length], i + length
            j = 0
            while j < len(entry):
                inner_tag, j = _read_varint(entry, j)
                if inner_tag is None:
                    break
                inner_wire, inner_field = inner_tag & 7, inner_tag >> 3
                if inner_field == 1 and inner_wire == 2:
                    size, j = _read_varint(entry, j)
                    categories.append(entry[j:j + size].decode("utf-8", "replace").lower())
                    break
                if inner_wire == 0:
                    _, j = _read_varint(entry, j)
                elif inner_wire == 2:
                    size, j = _read_varint(entry, j)
                    j += size
                else:
                    break
        elif wire == 0:
            _, i = _read_varint(data, i)
        elif wire == 2:
            length, i = _read_varint(data, i)
            if length is None:
                break
            i += length
        else:
            break
    return sorted(set(c for c in categories if c))


def categories(refresh=False):
    if not refresh and _CACHE.exists():
        try:
            cached = json.loads(_CACHE.read_text(encoding="utf-8"))
            if cached.get("geosite_mtime") == _mtime(settings.GEOSITE_DAT):
                return cached
        except (json.JSONDecodeError, OSError):
            pass

    result = {
        "geosite": _parse_categories(settings.GEOSITE_DAT),
        "geoip": _parse_categories(settings.GEOIP_DAT),
        "geosite_mtime": _mtime(settings.GEOSITE_DAT),
        "geoip_mtime": _mtime(settings.GEOIP_DAT),
    }
    try:
        _CACHE.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return result


def _mtime(path):
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return 0


def update():
    """Скачать свежие базы v2fly и сбросить кеш категорий."""
    downloaded = {}
    with httpx.Client(follow_redirects=True, timeout=120) as client:
        for name, url, target in (("geosite", GEOSITE_URL, settings.GEOSITE_DAT),
                                  ("geoip", GEOIP_URL, settings.GEOIP_DAT)):
            resp = client.get(url)
            resp.raise_for_status()
            tmp = target.with_suffix(".dat.tmp")
            tmp.write_bytes(resp.content)
            tmp.replace(target)
            downloaded[name] = len(resp.content)
    try:
        _CACHE.unlink(missing_ok=True)
    except OSError:
        pass
    downloaded["updated_at"] = int(time.time())
    return downloaded

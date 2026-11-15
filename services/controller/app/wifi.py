"""Точка доступа на USB WiFi: конфиг hostapd, запуск и присмотр за процессом.

hostapd крутится прямо в контейнере контроллера — как pppd: контейнер живёт в
host network namespace и privileged, поэтому nl80211 видит wlan-интерфейсы
хоста. Сам интерфейс включается в LAN-мост (см. netctl.lan_members), так что
wifi-клиенты получают DHCP, DNS и VPN-правила наравне с проводными.

Адаптер RTL8811CU приезжает в режиме CD-ROM (0bda:1a2b) и переводится в WLAN
(0bda:c811) правилом usb_modeswitch на хосте; имя wlan0 даёт systemd .link
(/etc/systemd/network/10-circle-wlan.link, Driver=rtw88_8821cu). Контроллер
этим не управляет — только пробует modprobe при отсутствии интерфейса и
объясняет в статусе, чего не хватает.
"""

import logging
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import netctl
from .shell import out

log = logging.getLogger("circle.wifi")

CONFIG_PATH = Path("/etc/hostapd/circle.conf")

# Драйверы wifi: индеревной rtw88_8821cu подхватывает адаптер после
# переключения из CD-ROM, rtl8xxxu — запасной для ревизий с ID 0bda:b711.
# mac80211/cfg80211 подтянутся по зависимостям, modprobe просто молчит,
# если что-то вкомпилировано в ядро.
WIFI_MODULES = ["mac80211", "rtl8xxxu", "rtw88_8821cu"]

_state = {
    "process": None,
    "thread": None,
    "want": False,
    # Поколение растёт на каждый start/stop — против дублей супервизоров
    # (тот же приём, что в pppoe).
    "generation": 0,
    "last_error": "",
    "restarts": 0,
    "started_at": 0.0,
}
_lock = threading.Lock()


def _write_config(cfg):
    """hostapd.conf. Значения в кавычках: иначе '#' в пароле/SSID съедается
    парсером hostapd как комментарий."""
    wifi = cfg["wifi"]
    iface = (wifi.get("interface") or "").strip() or netctl.first_wlan()
    lines = [
        "# Сгенерировано circle-router",
        f"interface={iface}",
        # Точка доступа живёт в LAN-мосте (или на LAN-порте, если свич выключен):
        # DHCP, DNS и TPROXY достаются wifi-клиентам без отдельных правил.
        f"bridge={netctl.lan_device(cfg)}",
        "driver=nl80211",
        f'ssid="{wifi.get("ssid", "")}"',
        "country_code=RU",
        "ieee80211d=1",
        # 2.4 ГГц, HT20: донгл 1x1, HT40 на заглушённом эфире только мешает.
        # channel=0 (hostapd ACS) rtw88 не тянет — «ACS: Failed to start», так
        # что пустое значение тихо превращается в 6-й канал.
        "hw_mode=g",
        f"channel={max(1, min(13, int(wifi.get('channel') or 6)))}",
        "ieee80211n=1",
        "wmm_enabled=1",
        "macaddr_acl=0",
        "auth_algs=1",
        "wpa=2",
        "wpa_key_mgmt=WPA-PSK",
        "rsn_pairwise=CCMP",
        f'wpa_passphrase="{wifi.get("password", "")}"',
    ]
    if wifi.get("hidden"):
        lines.append("ignore_broadcast_ssid=2")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _running_pids():
    try:
        proc = subprocess.run(["pgrep", "-f", f"hostapd {CONFIG_PATH}"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("pgrep недоступен: %s", exc)
        return []
    return [int(pid) for pid in proc.stdout.split() if pid.isdigit()]


def _kill_strays():
    """Добить hostapd прошлых запусков — только при остановке, как в pppoe."""
    try:
        subprocess.run(["pkill", "-f", f"hostapd {CONFIG_PATH}"],
                       capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("pkill недоступен: %s", exc)


def _supervise(generation):
    """hostapd работает в foreground; если умер — поднимаем заново."""
    while True:
        with _lock:
            if not _state["want"] or _state["generation"] != generation:
                return
        existing = _running_pids()
        if existing:
            log.info("hostapd уже работает (pid %s) — не поднимаю второй",
                     ", ".join(map(str, existing)))
            time.sleep(5)
            continue
        proc = subprocess.Popen(
            ["hostapd", str(CONFIG_PATH)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with _lock:
            _state["process"] = proc
            _state["started_at"] = time.time()
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            log.info("hostapd: %s", line)
            low = line.lower()
            if "could not" in low or "fail" in low or "error" in low:
                with _lock:
                    _state["last_error"] = line
        proc.wait()
        with _lock:
            if _state["process"] is proc:
                _state["process"] = None
            if not _state["want"] or _state["generation"] != generation:
                return
            _state["restarts"] += 1
            if not _state["last_error"]:
                _state["last_error"] = f"hostapd завершился (код {proc.returncode})"
        log.warning("hostapd завершился (код %s), перезапуск через 5 с", proc.returncode)
        time.sleep(5)


def start(cfg):
    iface = (cfg["wifi"].get("interface") or "").strip() or netctl.first_wlan()
    if not iface:
        # Драйвер мог не загрузиться на этом проходе — пробуем ещё раз,
        # прежде чем жаловаться. После usb_modeswitch интерфейс появляется
        # сам, и следующий «Применить» (или watchdog) поднимет точку.
        netctl.modprobe(WIFI_MODULES)
        iface = netctl.first_wlan()
    if not iface:
        with _lock:
            _state["last_error"] = ("wifi-адаптер не найден: нет wlan-интерфейса "
                                    "(драйвер? usb_modeswitch?)")
        log.warning("wifi: %s", _state["last_error"])
        return
    _write_config(cfg)
    with _lock:
        alive = _state["thread"] is not None and _state["thread"].is_alive()
        if _state["want"] and alive:
            return
        _state["want"] = True
        _state["generation"] += 1
        _state["last_error"] = ""
        generation = _state["generation"]
        thread = threading.Thread(target=_supervise, args=(generation,),
                                  name=f"hostapd-{generation}", daemon=True)
        _state["thread"] = thread
    thread.start()


def stop():
    with _lock:
        _state["want"] = False
        _state["generation"] += 1
        proc = _state["process"]
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    _kill_strays()


def apply(cfg):
    """Из reconcile: поднимает/гасит точку по конфигу. Возвращает строку для
    отчёта «Применить» или пустую строку, если сообщать нечего."""
    if not (cfg.get("wifi") or {}).get("enabled"):
        stop()
        return ""
    start(cfg)
    iface = (cfg["wifi"].get("interface") or "").strip() or netctl.first_wlan()
    if iface:
        return f"WiFi: точка доступа на {iface}"
    return "WiFi: адаптер не найден — точка доступа ждёт железо"


def _stations(iface):
    """Клиенты точки: `iw station dump` -> [{mac, signal, tx_bitrate}]."""
    text = out(["iw", "dev", iface, "station", "dump"])
    clients = []
    for line in text.splitlines():
        m = re.match(r"Station ([0-9a-f:]{17})\b", line)
        if m:
            clients.append({"mac": m.group(1), "signal": None, "tx_bitrate": None})
            continue
        if not clients:
            continue
        m = re.match(r"\s*signal:\s*(-?\d+)", line)
        if m:
            clients[-1]["signal"] = int(m.group(1))
            continue
        m = re.match(r"\s*tx bitrate:\s*([0-9.]+)", line)
        if m:
            clients[-1]["tx_bitrate"] = float(m.group(1))
    return clients


def status(cfg):
    """Всё для карточки WiFi: адаптер, точка, канал, клиенты, подсказки."""
    wifi = cfg.get("wifi") or {}
    iface = (wifi.get("interface") or "").strip() or netctl.first_wlan()
    with _lock:
        proc = _state["process"]
        running = bool(proc and proc.poll() is None)
        info = {
            "enabled": bool(wifi.get("enabled")),
            "running": running,
            "restarts": _state["restarts"],
            "last_error": _state["last_error"],
            "uptime": int(time.time() - _state["started_at"])
            if running and _state["started_at"] else 0,
            "interface": iface,
            "clients": [],
        }
    # Адаптер ещё в режиме CD-ROM — драйвер его не видит до переключения.
    info["cdrom_mode"] = "0bda:1a2b" in out(["lsusb"], default="")
    if iface:
        try:
            phy = (Path(f"/sys/class/net/{iface}/phy80211/name")).read_text().strip()
        except OSError:
            phy = ""
        phy_info = out(["iw", "phy", phy, "info"]) if phy else out(["iw", "phy"])
        info["ap_supported"] = "* AP" in phy_info
        dev_info = out(["iw", "dev", iface, "info"])
        m = re.search(r"channel (\d+) \((\d+) MHz\)", dev_info)
        info["channel"] = int(m.group(1)) if m else None
        info["frequency"] = int(m.group(2)) if m else None
        t = re.search(r"type (\w+)", dev_info)
        info["ap_up"] = bool(t and t.group(1).startswith("AP"))
        if running:
            info["clients"] = _stations(iface)
    else:
        info["ap_supported"] = False
        info["channel"] = None
        info["frequency"] = None
        info["ap_up"] = False
    return info


def validate(cfg):
    """Претензии к секции wifi — человекочитаемые строки в отчёт «Применить»."""
    wifi = cfg.get("wifi") or {}
    problems = []
    if not wifi.get("enabled"):
        return problems
    ssid = (wifi.get("ssid") or "").strip()
    if not 1 <= len(ssid) <= 32:
        problems.append("WiFi: имя сети (SSID) — от 1 до 32 символов")
    elif '"' in ssid:
        problems.append("WiFi: SSID не должен содержать кавычек")
    psk = wifi.get("password") or ""
    if not 8 <= len(psk) <= 63 or not psk.isascii():
        problems.append("WiFi: пароль — от 8 до 63 ASCII-символов (WPA2)")
    elif '"' in psk:
        problems.append("WiFi: пароль не должен содержать кавычек")
    channel = int(wifi.get("channel") or 0)
    if channel not in range(1, 14):
        problems.append("WiFi: канал — число 1–13 (авто-выбор у rtw88 не работает)")
    iface = (wifi.get("interface") or "").strip()
    if iface and not re.match(r"^wlan\d{1,2}$", iface):
        problems.append("WiFi: имя интерфейса — wlan<N> (или пусто для авто)")
    return problems

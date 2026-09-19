"""Подъём WAN по PPPoE: конфиг pppd, запуск и присмотр за процессом.

pppd крутится прямо в контейнере контроллера — тот в host network namespace и
privileged, поэтому созданный интерфейс ppp0 появляется на хосте.
"""

import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import netctl

log = logging.getLogger("circle.pppoe")

PPP_ETC = Path("/etc/ppp")
PEER_NAME = "circle-wan"
PEER_FILE = PPP_ETC / "peers" / PEER_NAME

_state = {
    "process": None,
    "thread": None,
    "want": False,
    # Поколение растёт на каждый start/stop. Супервизор с устаревшим номером
    # молча выходит — иначе после restart() старый и новый поднимут по сессии
    # каждый, и в системе окажутся ppp0 и ppp1 разом.
    "generation": 0,
    "last_error": "",
    "restarts": 0,
    "started_at": 0.0,
}
_lock = threading.Lock()


def _write_secrets(username, password):
    """PAP и CHAP: заранее не известно, что попросит концентратор провайдера."""
    line = f'"{username}" * "{password}" *\n'
    for name in ("pap-secrets", "chap-secrets"):
        path = PPP_ETC / name
        path.write_text(
            "# Сгенерировано circle-router\n"
            "# client\tserver\tsecret\tIP addresses\n" + line,
            encoding="utf-8",
        )
        os.chmod(path, 0o600)


def _write_peer(cfg):
    pppoe = cfg["wan"]["pppoe"]
    iface = cfg["wan"]["interface"]
    mtu = int(pppoe.get("mtu") or 1492)
    lines = [
        "# Сгенерировано circle-router",
        "plugin rp-pppoe.so",
        iface,
        f'user "{pppoe.get("username", "")}"',
        "noipdefault",
        "defaultroute",
        "replacedefaultroute",
        "noauth",
        "hide-password",
        "persist",
        "maxfail 0",
        "holdoff 5",
        f"mtu {mtu}",
        f"mru {mtu}",
        f"lcp-echo-interval {int(pppoe.get('lcp_echo_interval') or 20)}",
        f"lcp-echo-failure {int(pppoe.get('lcp_echo_failure') or 3)}",
    ]
    if pppoe.get("service"):
        lines.append(f'rp_pppoe_service {pppoe["service"]}')
    PEER_FILE.parent.mkdir(parents=True, exist_ok=True)
    PEER_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _write_secrets(pppoe.get("username", ""), pppoe.get("password", ""))


def _running_pids():
    """PID всех живых pppd нашей сессии — чужих в том числе."""
    try:
        proc = subprocess.run(["pgrep", "-f", f"pppd call {PEER_NAME}"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("pgrep недоступен: %s", exc)
        return []
    return [int(pid) for pid in proc.stdout.split() if pid.isdigit()]


def _kill_strays():
    """Добить pppd, оставшиеся от прошлых запусков.

    Две сессии к одному провайдеру дают два ppp-интерфейса с одним адресом и
    пляшущий маршрут по умолчанию. Вызываем только при остановке: на старте
    убивать вслепую нельзя — можно снести живую сессию соседнего процесса.
    """
    try:
        subprocess.run(["pkill", "-f", f"pppd call {PEER_NAME}"],
                       capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("pkill недоступен: %s", exc)


def _supervise(generation):
    """pppd запускается с nodetach; если он умер — поднимаем заново."""
    while True:
        with _lock:
            if not _state["want"] or _state["generation"] != generation:
                return
        # Последний рубеж против дубля: если pppd уже поднят кем-то ещё
        # (другим процессом, прошлым контейнером), ждём, а не плодим сессию.
        existing = _running_pids()
        if existing:
            log.info("pppd уже работает (pid %s) — не поднимаю вторую сессию",
                     ", ".join(map(str, existing)))
            time.sleep(5)
            continue
        proc = subprocess.Popen(
            ["pppd", "call", PEER_NAME, "nodetach", "logfd", "2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with _lock:
            _state["process"] = proc
            _state["started_at"] = time.time()
        for line in proc.stdout:
            line = line.strip()
            if line:
                log.info("pppd: %s", line)
                if "authentication failed" in line.lower() or "Failed" in line:
                    with _lock:
                        _state["last_error"] = line
        proc.wait()
        with _lock:
            if _state["process"] is proc:
                _state["process"] = None
            if not _state["want"] or _state["generation"] != generation:
                return
            _state["restarts"] += 1
        log.warning("pppd завершился (код %s), перезапуск через 5 с", proc.returncode)
        time.sleep(5)


def start(cfg):
    netctl.modprobe(netctl.PPPOE_MODULES)
    _write_peer(cfg)
    with _lock:
        alive = _state["thread"] is not None and _state["thread"].is_alive()
        if _state["want"] and alive:
            return False
        # Поток создаём и регистрируем под замком, а стартуем сразу после:
        # проверка на is_alive() до start() пропускала второй супервизор.
        _state["want"] = True
        _state["generation"] += 1
        _state["last_error"] = ""
        generation = _state["generation"]
        thread = threading.Thread(target=_supervise, args=(generation,),
                                  name=f"pppd-{generation}", daemon=True)
        _state["thread"] = thread
    thread.start()
    return True


def stop():
    with _lock:
        _state["want"] = False
        # Сдвигаем поколение: даже если старый супервизор сейчас спит перед
        # перезапуском, проснувшись он увидит чужой номер и выйдет.
        _state["generation"] += 1
        proc = _state["process"]
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    _kill_strays()
    return True


def restart(cfg):
    stop()
    time.sleep(1)
    return start(cfg)


def status():
    with _lock:
        proc = _state["process"]
        running = bool(proc and proc.poll() is None)
        info = {
            "running": running,
            "restarts": _state["restarts"],
            "last_error": _state["last_error"],
            "uptime": time.time() - _state["started_at"] if running and _state["started_at"] else 0,
        }
    iface = netctl.first_ppp()
    info["interface"] = iface
    info["interface_up"] = netctl.link_state(iface) in ("UP", "UNKNOWN")
    info["address"] = netctl.iface_ip(iface)
    return info

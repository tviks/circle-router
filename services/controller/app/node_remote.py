"""Что выполняется на самой VDS. Только POSIX sh и systemd.

На сервер ставится ровно одно — релизный бинарь xray-core той же версии, что
и на роутере (Reality сверяет версию клиента), плюс systemd-unit и пара
sysctl'ей. Никаких панелей, агентов и контейнеров: метрики роутер забирает
сам, читая /proc по SSH.
"""

import json
import logging
import re
import shlex
import time

from . import node_build as nb
from .shell import out
from . import settings

log = logging.getLogger("circle.node")

_router_xray_version = None


def router_xray_version():
    """Версия ядра на роутере: нода ставится такой же."""
    global _router_xray_version
    if _router_xray_version is None:
        text = out([settings.XRAY_BIN, "version"], timeout=10)
        match = re.search(r"Xray\s+(\d+\.\d+\.\d+)", text or "")
        _router_xray_version = f"v{match.group(1)}" if match else "v26.3.27"
    return _router_xray_version


BOOTSTRAP = r"""
set -eu
VER="$1"
log() { echo "[circle] $*"; }

command -v systemctl >/dev/null 2>&1 || { echo "нужен systemd" >&2; exit 10; }

case "$(uname -m)" in
  x86_64|amd64) ZIP=Xray-linux-64.zip ;;
  aarch64|arm64) ZIP=Xray-linux-arm64-v8a.zip ;;
  armv7*|armv8l) ZIP=Xray-linux-arm32-v7a.zip ;;
  *) echo "архитектура $(uname -m) не поддерживается" >&2; exit 11 ;;
esac

need=""
command -v curl >/dev/null 2>&1 || need="$need curl"
command -v unzip >/dev/null 2>&1 || need="$need unzip"
if [ -n "$need" ]; then
  log "ставлю:$need"
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq $need ca-certificates >/dev/null
  elif command -v dnf >/dev/null 2>&1; then dnf -y -q install $need ca-certificates >/dev/null
  elif command -v yum >/dev/null 2>&1; then yum -y -q install $need ca-certificates >/dev/null
  else echo "не знаю, как поставить$need" >&2; exit 12
  fi
fi

mkdir -p /usr/local/lib/circle-node /usr/local/etc/circle-node
current="$(/usr/local/lib/circle-node/xray version 2>/dev/null | awk 'NR==1{print $2}' || true)"
if [ "v$current" != "$VER" ]; then
  log "качаю xray $VER ($ZIP)"
  tmp="$(mktemp -d)"
  base="https://github.com/XTLS/Xray-core/releases/download/$VER/$ZIP"
  curl -fsSL --retry 3 --connect-timeout 15 -o "$tmp/x.zip" "$base"
  if curl -fsSL --retry 2 --connect-timeout 15 -o "$tmp/x.dgst" "$base.dgst"; then
    want="$(grep '^SHA2-256' "$tmp/x.dgst" | head -n1 | awk '{print $NF}')"
    got="$(sha256sum "$tmp/x.zip" | awk '{print $1}')"
    if [ -n "$want" ] && [ "$want" != "$got" ]; then
      echo "контрольная сумма архива не совпала" >&2; rm -rf "$tmp"; exit 13
    fi
  else
    log "нет файла контрольной суммы, проверку пропускаю"
  fi
  unzip -o -q "$tmp/x.zip" xray -d "$tmp"
  install -m 0755 "$tmp/xray" /usr/local/lib/circle-node/xray
  rm -rf "$tmp"
else
  log "xray $VER уже стоит"
fi

if ! id circle-node >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin circle-node 2>/dev/null \
    || useradd -r -M -s /sbin/nologin circle-node
fi
chown root:circle-node /usr/local/etc/circle-node
chmod 0750 /usr/local/etc/circle-node

# BBR + fq и буферы под длинный жирный канал. Никаких экзотических ручек.
modprobe tcp_bbr 2>/dev/null || true
cat > /etc/sysctl.d/90-circle-node.conf <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_slow_start_after_idle = 0
net.ipv4.tcp_notsent_lowat = 16384
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 131072 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.core.somaxconn = 4096
net.ipv4.ip_local_port_range = 10240 65000
EOF
sysctl -q --system >/dev/null 2>&1 || true

cat > /etc/systemd/system/circle-node.service <<'EOF'
[Unit]
Description=circle-router node (xray-core)
After=network-online.target
Wants=network-online.target

[Service]
User=circle-node
Group=circle-node
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ExecStart=/usr/local/lib/circle-node/xray run -config /usr/local/etc/circle-node/config.json
Restart=always
RestartSec=2
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable circle-node >/dev/null 2>&1 || true

. /etc/os-release 2>/dev/null || true
echo "OS=${PRETTY_NAME:-unknown}"
echo "ARCH=$(uname -m)"
echo "CC=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null || echo '?')"
echo "CPUS=$(nproc 2>/dev/null || echo 1)"
"""


def bootstrap(session, version):
    _, output = session.run(f"sh -s {shlex.quote(version)}", input_data=BOOTSTRAP, timeout=600)
    facts = {}
    for line in output.splitlines():
        if "=" in line and line.split("=", 1)[0] in ("OS", "ARCH", "CC", "CPUS"):
            key, value = line.split("=", 1)
            facts[key.lower()] = value.strip()
    return facts, output


def install_cert(session, node):
    s = node["secrets"]
    owner = f"root:{nb.SERVICE_USER}"
    session.write_file(nb.REMOTE_CERT, s["hy2_cert"], mode="0640", owner=owner)
    session.write_file(nb.REMOTE_KEY, s["hy2_key"], mode="0640", owner=owner)


def check_sni(session, domains):
    """Какие маскировочные домены отвечают с самой ноды и как быстро.

    `xray tls ping` делает настоящий TLS-handshake с SNI. Reality ходит на
    этот адрес при каждом неавторизованном подключении, так что домен должен
    быть доступен именно с сервера, а не с роутера.
    """
    results = []
    for domain in domains:
        script = (f"s=$(date +%s%N); out=$(timeout 8 {nb.REMOTE_BIN} tls ping "
                  f"{shlex.quote(domain)} 2>&1); rc=$?; e=$(date +%s%N); "
                  f"echo \"$out\"; echo \"@@rc=$rc ms=$(( (e - s) / 1000000 ))\"")
        try:
            _, output = session.run(script, root=False, timeout=20, check=False)
        except Exception as exc:                       # noqa: BLE001
            results.append({"domain": domain, "ok": False, "error": str(exc)})
            continue
        match = re.search(r"@@rc=(\d+) ms=(\d+)", output)
        rc = int(match.group(1)) if match else 1
        ms = int(match.group(2)) if match else None
        with_sni = output.split("with SNI")[-1].lower() if "with SNI" in output else output.lower()
        ok = rc == 0 and "succeeded" in with_sni
        tls12 = bool(re.search(r"tls\s*(version)?[:\s]*1\.2", with_sni))
        results.append({"domain": domain, "ok": ok and not tls12, "ms": ms,
                        "error": None if ok else ("только TLS 1.2" if tls12 else "handshake не прошёл")})
    return results


def _firewall_cmd(ports, allow):
    """Открыть/закрыть порты в ufw или firewalld, если они включены. На
    голом iptables с политикой DROP никто не живёт — его не трогаем."""
    lines = []
    for network, port in ports:
        if allow:
            lines.append(f"ufw allow {port}/{network} comment circle-node >/dev/null 2>&1 || true")
        else:
            lines.append(f"ufw delete allow {port}/{network} >/dev/null 2>&1 || true")
    ufw = "\n    ".join(lines) or ":"
    fw_lines = []
    for network, port in ports:
        op = "--add-port" if allow else "--remove-port"
        fw_lines.append(f"firewall-cmd -q {op}={port}/{network} || true; "
                        f"firewall-cmd -q --permanent {op}={port}/{network} || true")
    fwd = "\n    ".join(fw_lines) or ":"
    return f"""
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q 'Status: active'; then
    {ufw}
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    {fwd}
fi
"""


def open_ports(session, ports):
    session.run(_firewall_cmd(ports, True), timeout=60, check=False)


def close_ports(session, ports):
    if ports:
        session.run(_firewall_cmd(ports, False), timeout=60, check=False)


def _test_config(session, path):
    rc, output = session.run(f"{nb.REMOTE_BIN} run -test -config {path}",
                             timeout=60, check=False)
    return rc == 0, output.strip()


def stop_probe(session):
    session.run(f"systemctl stop {nb.PROBE_SERVICE} >/dev/null 2>&1; "
                f"systemctl reset-failed {nb.PROBE_SERVICE} >/dev/null 2>&1; true",
                timeout=30, check=False)


def start_probe(session, node, specs):
    """Временный второй экземпляр ядра с пробными инбаундами.

    Боевой circle-node при этом не трогается: клиенты на нём ничего не
    замечают. RuntimeMaxSec гасит пробу сам, даже если роутер пропал посреди
    подбора и не успел её остановить.
    """
    stop_probe(session)
    config = nb.server_config(node, specs)
    session.write_file(nb.REMOTE_PROBE, json.dumps(config, indent=2),
                       mode="0640", owner=f"root:{nb.SERVICE_USER}")
    ok, output = _test_config(session, nb.REMOTE_PROBE)
    if not ok:
        raise RuntimeError(f"ядро не приняло пробный конфиг: {output[-400:]}")
    session.run(
        f"systemd-run --quiet --unit={nb.PROBE_SERVICE} --collect "
        f"-p RuntimeMaxSec=900 -p User={nb.SERVICE_USER} -p Group={nb.SERVICE_USER} "
        f"-p AmbientCapabilities=CAP_NET_BIND_SERVICE -p LimitNOFILE=65535 "
        f"{nb.REMOTE_BIN} run -config {nb.REMOTE_PROBE}", timeout=30)
    time.sleep(1.5)
    rc, _ = session.run(f"systemctl is-active --quiet {nb.PROBE_SERVICE}",
                        timeout=15, check=False)
    if rc != 0:
        _, journal = session.run(f"journalctl -u {nb.PROBE_SERVICE} -n 20 --no-pager",
                                 timeout=15, check=False)
        raise RuntimeError(f"пробный экземпляр не запустился: {journal.strip()[-400:]}")


def install_production(session, node, spec):
    """Боевой конфиг: один инбаунд — победитель подбора."""
    config = nb.server_config(node, [spec])
    tmp = f"{nb.REMOTE_CONFIG}.candidate.json"
    session.write_file(tmp, json.dumps(config, indent=2),
                       mode="0640", owner=f"root:{nb.SERVICE_USER}")
    ok, output = _test_config(session, tmp)
    if not ok:
        session.run(f"rm -f {tmp}", check=False)
        raise RuntimeError(f"ядро не приняло конфиг: {output[-400:]}")
    session.run(f"mv -f {tmp} {nb.REMOTE_CONFIG} && systemctl restart {nb.SERVICE}", timeout=60)
    time.sleep(1.5)
    rc, _ = session.run(f"systemctl is-active --quiet {nb.SERVICE}", timeout=15, check=False)
    if rc != 0:
        _, journal = session.run(f"journalctl -u {nb.SERVICE} -n 20 --no-pager",
                                 timeout=15, check=False)
        raise RuntimeError(f"ядро на ноде не запустилось: {journal.strip()[-400:]}")


def restart(session):
    session.run(f"systemctl restart {nb.SERVICE}", timeout=60)


def uninstall(session):
    session.run(
        f"systemctl disable --now {nb.SERVICE} >/dev/null 2>&1; "
        f"systemctl stop {nb.PROBE_SERVICE} >/dev/null 2>&1; "
        f"rm -f /etc/systemd/system/{nb.SERVICE}.service /etc/sysctl.d/90-circle-node.conf; "
        f"systemctl daemon-reload; rm -rf {nb.REMOTE_DIR} {nb.REMOTE_ETC}; "
        f"userdel {nb.SERVICE_USER} >/dev/null 2>&1; true", timeout=120, check=False)

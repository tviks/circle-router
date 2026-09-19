#!/bin/sh
# Ждём, пока controller сгенерирует конфиг, и только потом стартуем xray.
# Без этого контейнер уходит в crash-loop на первом же запуске стека.
set -eu

CONFIG=/etc/xray/config.json
mkdir -p /var/log/xray /usr/share/xray

for f in geoip.dat geosite.dat; do
    if [ ! -s "/usr/share/xray/$f" ] && [ -s "/opt/geo-seed/$f" ]; then
        cp "/opt/geo-seed/$f" "/usr/share/xray/$f"
    fi
done

waited=0
while [ ! -s "$CONFIG" ]; do
    if [ "$waited" -eq 0 ]; then
        echo "xray: жду появления $CONFIG (его пишет controller)…"
    fi
    waited=$((waited + 1))
    sleep 1
done

if ! /usr/local/bin/xray run -test -config "$CONFIG" >/tmp/xray-test.log 2>&1; then
    echo "xray: конфиг не прошёл проверку:" >&2
    cat /tmp/xray-test.log >&2
    exit 1
fi

exec /usr/local/bin/xray run -config "$CONFIG"

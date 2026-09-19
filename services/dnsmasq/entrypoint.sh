#!/bin/sh
# dnsmasq с bind-interfaces падает, если LAN-адреса ещё нет в системе, поэтому
# ждём и конфиг от controller'а, и поднятый LAN-интерфейс с адресом.
set -eu

CONFIG=/etc/dnsmasq/dnsmasq.conf
mkdir -p /var/log/dnsmasq /etc/dnsmasq

waited=0
while [ ! -s "$CONFIG" ]; do
    [ "$waited" -eq 0 ] && echo "dnsmasq: жду появления $CONFIG (его пишет controller)…"
    waited=$((waited + 1))
    sleep 1
done

# listen-address в конфиге — это и есть адрес, который должен существовать.
ADDR=$(sed -n 's/^listen-address=\([0-9.]*\).*/\1/p' "$CONFIG" | head -n1)
if [ -n "${ADDR:-}" ]; then
    waited=0
    while ! ip -4 -o addr show | grep -q " ${ADDR}/"; do
        [ "$waited" -eq 0 ] && echo "dnsmasq: жду адрес ${ADDR} на LAN-интерфейсе…"
        waited=$((waited + 1))
        [ "$waited" -gt 300 ] && break
        sleep 1
    done
fi

exec dnsmasq --keep-in-foreground --log-facility=- --conf-file="$CONFIG"

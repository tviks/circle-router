#!/usr/bin/env bash
# Аварийный сброс: снимает все правила circle-router, LAN уходит в прямой роутинг.
# Пригодится, если правки в фаерволе или TPROXY оборвали доступ.
set -euo pipefail

if docker exec circle-controller /app/panic.sh 2>/dev/null; then
    exit 0
fi

echo "Контроллер недоступен — чищу таблицы напрямую." >&2
for spec in "inet circle_filter" "ip circle_nat" "ip circle_mangle" "inet circle_acct"; do
    # shellcheck disable=SC2086
    sudo nft delete table $spec 2>/dev/null || true
done
while sudo ip rule del fwmark 1 table 100 2>/dev/null; do :; done
sudo ip route flush table 100 2>/dev/null || true
echo "Правила circle-router сняты."

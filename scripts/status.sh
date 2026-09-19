#!/usr/bin/env bash
# Быстрая диагностика: что сейчас с роутингом, правилами и сервисами.
set -uo pipefail

echo "── контейнеры ───────────────────────────────────────────────"
docker compose ps 2>/dev/null || docker ps --filter name=circle-

echo
echo "── адреса ───────────────────────────────────────────────────"
ip -br addr

echo
echo "── маршруты по умолчанию ────────────────────────────────────"
ip route show default

echo
echo "── policy routing (TPROXY) ──────────────────────────────────"
ip rule show | grep -E "fwmark" || echo "правил с fwmark нет — VPN не активен"

echo
echo "── таблицы nftables circle-router ───────────────────────────"
sudo nft list tables 2>/dev/null | grep circle_ || echo "таблиц circle_* нет"

echo
echo "── xray ─────────────────────────────────────────────────────"
docker exec circle-xray /usr/local/bin/xray api statsquery \
    --server=127.0.0.1:10085 -pattern="outbound" 2>&1 | head -30

#!/bin/sh
# Аварийный сброс: снимает все правила circle-router и возвращает прямой роутинг.
# Запуск с хоста:  docker exec circle-controller /app/panic.sh
set -eu
python -c "from app import firewall, netctl, state; c = state.load(); firewall.teardown(); netctl.tproxy_routing_down(int(c['tproxy']['fwmark']), int(c['tproxy']['table'])); print('правила circle-router сняты')"

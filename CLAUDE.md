# circle-router — база знаний для агента

Прозрачный VPN-роутер на мини-ПК (2×ethernet), всё в Docker Compose. Подробности
и грабли — в README.md (раздел «Грабли, которые здесь уже обойдены» обязателен
к прочтению перед правками сети). Язык проекта, комментариев и UI — русский.

## Карта

```
docker-compose.yml        3 сервиса, все network_mode: host
services/xray/            образ xray-core (TPROXY-инбаунд, аутбаунд proxy)
services/dnsmasq/         DHCP + DNS + блок-лист
services/controller/      FastAPI-мозг, privileged; в образе есть свой бинарь xray
  app/main.py             все REST-эндпоинты + раздача SPA из ./static
  app/state.py            router.json — единственный источник правды; _default_config()
                          дописывает недостающие ключи (миграций нет — просто новые ключи)
  app/apply.py            reconcile() — полный проход сеть→nft→конфиги→рестарты; Watchdog
                          (раз в 1 с решает, включён ли TPROXY)
  app/xray_build.py       сборка /etc/xray/config.json, validate() через `xray run -test`
  app/xray_url.py         share-ссылки ↔ outbound
  app/firewall.py         nftables (свои таблицы circle_*), DOCKER-USER
  app/netctl.py           sysctl, ip rule/route, адреса
  app/stats.py            сэмплер метрик (2 с), SQLite-история
  app/speedtest.py        замер через конкретный сервер (временный xray + HTTP-инбаунд)
  app/tunnel.py           монитор живости туннеля → failover на прямой интернет
  app/ssh.py              paramiko-сессии к VDS, TOFU host key, ключ роутера
  app/node_build.py       секреты, кандидаты протоколов, серверные/клиентские конфиги
  app/node_remote.py      что выполняется на VDS (bootstrap sh, проба systemd-run, прод)
  app/node_tune.py        подбор протокола: Tester + раунды A–D, score()
  app/nodes.py            задачи (deploy/tune/apply/restart/uninstall), советник
  app/node_monitor.py     метрики нод: поток `cat /proc` по SSH
web/                      React + Vite + Tailwind; страницы в src/pages, UI-кит в
                          src/components/ui.jsx, API-клиент src/lib/api.js
```

## Как устроено главное

- Трафик LAN → nft TPROXY → xray `tproxy-in` → `proxy`/`direct`. Watchdog снимает
  TPROXY, если xray не слушает порт ИЛИ (failover включён и) `tunnel.monitor` не
  в состоянии `up`. Состояние watchdog: `up` / `down` / `failover`.
- `health-in` — HTTP-инбаунд xray на 127.0.0.1:10809, всегда → `proxy`. Через него
  ходят монитор туннеля и SSH метрик нод (внутри туннеля).
- Сервер в `cfg.servers` может иметь `outbound` + `extra_outbounds` (у нод —
  freedom-фрагментатор, к которому основной аутбаунд цепляется `sockopt.dialerProxy`).
  `xray_build.extra_outbounds_for()` добавляет их и в боевой конфиг, и в speedtest.
- Нода: `cfg.nodes[]` (секреты внутри; наружу — только `nodes.public()`, а
  `GET /api/config` вырезает `nodes`). Кандидат = {transport, port, sni, fp, frag};
  ключ `node_build.cand_key()` (продублирован в Nodes.jsx как candKey).
- Длинные операции — фоновые потоки с журналом (`nodes._jobs`), UI опрашивает.

## Проверено на практике (xray 26.3.27)

- Reality: `chrome/firefox/safari/edge/ios` работают, `android` — нет,
  `randomized` — через раз. Hysteria2 c `pinnedPeerCertSha256` (hex SHA-256 DER) — ок.
- `xray tls ping` всегда rc=0 — успех по тексту `Handshake succeeded` после «with SNI».
- `.dgst`: строка `SHA2-256= <hex>`.
- Reality на не-443 порту — ядро пишет warning про бан IP; gRPC и SS помечены deprecated.

## Локальная проверка без роутера (Windows)

- Python 3.12; зависимости ставить в venv (в системе fastapi нет).
- xray для Windows: `Xray-windows-64.zip` из релизов той же версии → `CR_XRAY_BIN`.
- E2E туннелей: `node_build.server_config()` запускаем локальным xray, клиентов —
  `node_tune.Tester` с host=127.0.0.1. Подбор целиком — подменить
  `node_remote.start_probe/stop_probe/open_ports/close_ports/check_sni`.
- На Windows не работают: docker.sock, `os.getloadavg`, TPROXY-sockopt, путь лога
  `/var/log/xray` — для превью их заглушать.
- Фронт: `cd web && npx vite build`. WSL Ubuntu 24.04 есть — годится для `sh -n` и
  чтения /proc, ничего в неё не ставить без спроса.

## Правила работы

- Сеть на роутере — боевая: правки, рвущие интернет, делать с планом отката;
  сборку образов — до остановки старых контейнеров (`docker compose build`, потом `up -d`).
- Не ставить `flush ruleset`, не трогать `policy` Docker-цепочек (см. README).
- Комментарии в коде объясняют «почему», в стиле окружающего кода.

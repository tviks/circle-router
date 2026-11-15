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
  app/netctl.py           sysctl, ip rule/route, адреса, мост свич-портов (br-lan)
  app/stats.py            сэмплер метрик (2 с), SQLite-история
  app/speedtest.py        замер через конкретный сервер (временный xray + HTTP-инбаунд)
  app/tunnel.py           монитор живости туннеля → failover на прямой интернет
  app/ssh.py              paramiko-сессии к VDS, TOFU host key, ключ роутера
  app/node_build.py       секреты, кандидаты протоколов, серверные/клиентские конфиги
  app/node_remote.py      что выполняется на VDS (bootstrap sh, проба systemd-run, прод)
  app/node_tune.py        подбор протокола: Tester + раунды A–D, score()
  app/nodes.py            задачи (deploy/tune/apply/restart/uninstall), советник
  app/node_monitor.py     метрики нод: поток `cat /proc` по SSH
  app/proxylists.py       умный прокси: списки доменов/подсетей → dnsmasq nftset + nft-сеты
  app/wanspeed.py         общий спидтест (curl, 4 потока; direct и через health-in)
  app/wifi.py             точка доступа: hostapd-супервизор (как pppd), статус
                          и клиенты через `iw`, валидация секции wifi
web/                      React + Vite + Tailwind; страницы в src/pages, UI-кит в
                          src/components/ui.jsx, API-клиент src/lib/api.js
```

## Как устроено главное

- Трафик LAN → nft TPROXY → xray `tproxy-in` → `proxy`/`direct`. UDP идёт тем же
  путём, что TCP: отдельного правила «UDP напрямую» нет. `block_quic` выключен
  по умолчанию; своим Vision-нодам нужен flow `xtls-rprx-vision-udp443`, иначе
  xray сам отбрасывает UDP/443. Импортированным ссылкам flow не переписываем:
  sing-box-серверы проверяют строку и отвергли бы соединение целиком.
- WireGuard — userspace в xray-core (и инбаунд, и аутбаунд), без wg-quick. В
  контейнере клиентский аутбаунд всегда с `noKernelTun: true` (нет
  /dev/net/tun, но есть NET_ADMIN — иначе ядро выберет TUN и упадёт).
  `xray_url.parse_url` понимает `wireguard://` и `.conf` `[Interface]/[Peer]`.
- Watchdog снимает TPROXY, если xray не слушает порт ИЛИ (failover включён и)
  `tunnel.monitor` не в состоянии `up`. Состояние watchdog: `up` / `down` /
  `failover`.
- `self_vpn` — заворот собственного трафика роутера (apt/docker/curl из
  консоли) в туннель: output-цепочка в `circle_mangle` метит пакеты fwmark'ом
  TPROXY → policy routing на lo → prerouting-правило `iif lo` отдаёт их в тот
  же tproxy-инбаунд. Аутбаундам xray проставляется `sockopt.mark 0xff`
  (`settings.XRAY_MARK`) — до этого mark'ов у xray принципиально не было;
  output-цепочка выпускает их первым правилом, иначе петля. Мимо заворота:
  icmp, порт 53, DHCP (sport 67), bypass4, wireguard-эндпоинты (сет
  `selfvpn_skip`; доменный WG-эндпоинт при self_vpn требует IP — см.
  `xray_build.selfvpn_problems`). Снимается watchdog'ом синхронно с TPROXY.
- WiFi (`wifi` в конфиге, вкладка WiFi): hostapd в контейнере контроллера
  (nl80211 хоста виден из host netns), wlan-интерфейс — член `br-lan`
  (`netctl.lan_members` добавляет его при `wifi.enabled`), клиенты = обычные
  клиенты LAN. Супервизор — по образцу pppoe (поколения, рестарт 5 с).
  ACS у rtw88 не работает — канал только фиксированный 1–13. Адаптер
  RTL8811CU из CD-ROM (`0bda:1a2b`) переводит usb_modeswitch на хосте
  (`/etc/usb_modeswitch.d/0bda:1a2b`), имя wlan0 даёт systemd .link
  (`Driver=rtw88_8821cu`); в образе контроллера нужны hostapd, iw, usbutils.
- `health-in` — HTTP-инбаунд xray на 127.0.0.1:10809, всегда → `proxy`. Через него
  ходят монитор туннеля и SSH метрик нод (внутри туннеля).
- Сервер в `cfg.servers` может иметь `outbound` + `extra_outbounds` (у нод —
  freedom-фрагментатор, к которому основной аутбаунд цепляется `sockopt.dialerProxy`).
  `xray_build.extra_outbounds_for()` добавляет их и в боевой конфиг, и в speedtest.
- Нода: `cfg.nodes[]` (секреты внутри; наружу — только `nodes.public()`, а
  `GET /api/config` вырезает `nodes`). Кандидат = {transport, port, sni, fp, frag};
  ключ `node_build.cand_key()` (продублирован в Nodes.jsx как candKey).
- Длинные операции — фоновые потоки с журналом (`nodes._jobs`), UI опрашивает.
- `bypass_torrents` (EDITABLE, вкл по умолчанию): правило `protocol: bittorrent`
  → `direct` в `xray_build.routing_rules`, до `force_proxy`. Сниффер уже есть
  (routeOnly); опознаёт DHT/uTP по содержимому, не по портам.
- Бэкап: `GET /api/backup` — весь router.json С секретами (pppoe, nodes),
  `POST /api/restore` — замена + reconcile; нормализация через `state.normalize`,
  валидация как у `POST /api/config` (problems → ок:false, без записи).
- Резервации DHCP (`dhcp.reservations`): «булавка» на странице Клиентов делает
  patchConfig+apply одним кликом; `dnsmasq_cfg.validate` проверяет формат MAC
  (`aa:bb:cc:dd:ee:ff`) и дубли MAC/IP.
- Свич-порты (`lan.switch_ports`, на платах с >2 ethernet): собираются мостом
  `br-lan` с LAN-портом, адрес и dnsmasq живут на мосту. Всюду, где нужно имя
  LAN-девайса, брать `netctl.lan_device(cfg)`, а не `cfg["lan"]["interface"]`
  (тот всегда физический порт). На платах с двумя портами мост не создаётся.

## Боевой роутер 192.168.11.1 (aarch64, 3 порта: wan1/lan1/lan2)

- Проект в `/root/circle-router` (git-клон ветки `self_hosted_node`; фичи лежат
  несокоммиченными и локально, и на плате — деплой = tar изменённых файлов).
  WAN = `lan2` (PPPoE `ppp0` поверх него), LAN = `lan1` 192.168.11.1/24,
  `wan1` свободен — кандидат в свич-порты. `.env` и `data/` не трогать.
- Включён умный прокси (`smart_proxy.enabled`), flowtable, RPS. Сборка образов
  идёт прямо на плате (vite build — десятки секунд, слои docker кэшируются).
- Хост резолвит через systemd-resolved → `/etc/systemd/resolved.conf.d/circle.conf`
  (публичные 1.1.1.1/8.8.8.8). После ребута платы конфиг может исчезнуть — без него
  сборка docker падает на резолве registry-1.docker.io (см. README «DNS самого роутера»).
- Из Git Bash к роутеру: `circle-scratch/pi.py` с `CR_SSH_HOST=192.168.11.1`
  (`python -I pi.py exec/put/get`); пути вида `/tmp/x` в аргументах портит MSYS —
  ставить `MSYS_NO_PATHCONV=1`, команда — с `< /dev/null`. Часы платы могут
  отставать от локальных — tar ругается на timestamps в будущем, это не страшно.

## Проверено на практике (xray 26.3.27)

- Reality: `chrome/firefox/safari/edge/ios` работают, `android` — нет,
  `randomized` — через раз. Hysteria2 c `pinnedPeerCertSha256` (hex SHA-256 DER) — ок.
- `xray tls ping` всегда rc=0 — успех по тексту `Handshake succeeded` после «with SNI».
- `.dgst`: строка `SHA2-256= <hex>`.
- Reality на не-443 порту — ядро пишет warning про бан IP; gRPC и SS помечены deprecated.
- Balancer с `fallbackTag` + observatory: до первой пробы трафик идёт в fallback,
  после — в живой selector; при мёртвом selector — в fallback. Проверено локально.
- С этого Windows-ПК наружу не ходит обычный DNS (UDP/TCP 53) — в локальных
  тестах резолвера использовать DoH.

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

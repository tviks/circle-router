# syntax=docker/dockerfile:1
# Контекст сборки — корень проекта: нужны и web/, и services/controller/.

# ── 1. Сборка веб-панели ─────────────────────────────────────────────────────
FROM node:22-alpine AS web
WORKDIR /build
COPY web/package.json ./
RUN npm install --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ── 2. Бинарь xray: нужен контроллеру для `xray api statsquery` ──────────────
FROM debian:bookworm-slim AS xraybin
ARG XRAY_VERSION=v26.3.27
ARG TARGETARCH=amd64
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) zip="Xray-linux-64.zip" ;; \
      arm64) zip="Xray-linux-arm64-v8a.zip" ;; \
      arm)   zip="Xray-linux-arm32-v7a.zip" ;; \
      *) echo "unsupported arch ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/xray.zip \
      "https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/${zip}"; \
    unzip -o /tmp/xray.zip -d /tmp/xray; \
    mkdir -p /out; \
    install -m 0755 /tmp/xray/xray /out/xray

# ── 3. Рантайм ───────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

# nftables/iproute2 — правила и роутинг на хосте; ppp + pppoe — WAN по PPPoE;
# kmod — modprobe для nf_tproxy и pppoe.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nftables iproute2 iputils-ping kmod procps ppp pppoe ca-certificates \
      conntrack tcpdump \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY services/controller/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY services/controller/app ./app
COPY services/controller/panic.sh ./panic.sh
COPY --from=web /build/dist ./static
COPY --from=xraybin /out/xray /usr/local/bin/xray
RUN chmod +x ./panic.sh

# `xray run -test` инициализирует логгер по пути из конфига — без каталога
# проверка падает не по делу.
RUN mkdir -p /var/log/xray

ENV PYTHONUNBUFFERED=1 CR_DATA=/data CR_HTTP_PORT=8088
EXPOSE 8088
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${CR_HTTP_PORT} --proxy-headers"]

# ── Скачиваем релизный бинарь xray-core и свежие geo-базы ────────────────────
FROM debian:bookworm-slim AS fetch
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
    mkdir -p /out/bin /out/geo; \
    unzip -o /tmp/xray.zip -d /tmp/xray; \
    install -m 0755 /tmp/xray/xray /out/bin/xray; \
    curl -fsSL -o /out/geo/geoip.dat \
      https://github.com/v2fly/geoip/releases/latest/download/geoip.dat; \
    curl -fsSL -o /out/geo/geosite.dat \
      https://github.com/v2fly/domain-list-community/releases/latest/download/dlc.dat

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=fetch /out/bin/xray /usr/local/bin/xray
# Эталонные geo-базы. entrypoint кладёт их в /usr/share/xray, если тома пустые,
# чтобы обновлённые из панели файлы не затирались при каждом рестарте.
COPY --from=fetch /out/geo/ /opt/geo-seed/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
VOLUME ["/etc/xray", "/usr/share/xray", "/var/log/xray"]
ENTRYPOINT ["/entrypoint.sh"]

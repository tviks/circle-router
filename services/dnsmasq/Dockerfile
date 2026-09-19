FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends dnsmasq iproute2 \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/dnsmasq.d/*
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
VOLUME ["/etc/dnsmasq", "/var/log/dnsmasq"]
ENTRYPOINT ["/entrypoint.sh"]

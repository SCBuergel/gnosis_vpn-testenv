# Ubuntu 24.04-based client image for binaries built against glibc (cargo builds, the .deb package).
# The upstream image (gnosis_vpn-client/docker/Dockerfile) is Alpine and needs the static Nix build.
# Extras over upstream: python3 (suite probes), curl, iproute2, iputils-ping, procps.
FROM ubuntu:24.04
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      wireguard-tools iproute2 iputils-ping curl python3 procps ca-certificates >/dev/null \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --system --home-dir /var/lib/gnosisvpn --shell /usr/sbin/nologin gnosisvpn
WORKDIR /app
COPY gnosis_vpn-root gnosis_vpn-worker gnosis_vpn-ctl entrypoint.sh ./
RUN chmod 755 gnosis_vpn-root gnosis_vpn-worker gnosis_vpn-ctl entrypoint.sh \
    && ln -s /app/gnosis_vpn-ctl /usr/local/bin/gnosis_vpn-ctl
ENTRYPOINT ["./entrypoint.sh"]

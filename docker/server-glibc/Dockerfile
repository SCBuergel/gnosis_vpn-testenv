# Ubuntu 24.04-based exit-server image for a glibc build of gnosis_vpn-server (cargo build --release).
# Mirrors gnosis_vpn-server/docker/Dockerfile (Alpine + static Nix build) otherwise.
FROM ubuntu:24.04
RUN apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
      wireguard-tools iptables iproute2 procps ca-certificates >/dev/null \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY config.toml wggvpn.conf gnosis_vpn-server wrapper.sh ./
RUN chmod 755 gnosis_vpn-server wrapper.sh
EXPOSE 8000/tcp 51820/udp
CMD ["./wrapper.sh"]

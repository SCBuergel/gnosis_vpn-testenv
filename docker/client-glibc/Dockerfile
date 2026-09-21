# Ubuntu 24.04-based client image for binaries built against glibc: a `cargo build --release` of any commit, or the
# binaries unpacked from the .deb package.
# The release configuration is the upstream image (gnosis_vpn-client/docker/Dockerfile: Alpine + the static Nix
# build), and `just build-client` builds exactly that; the suite's default stack uses it. This image is NOT a release
# configuration and is not what a run should be reported against. It exists for one job: bisecting or A/B-ing
# client commits on a host where a cargo build takes minutes and the Nix build does not, and for exercising the
# .deb (which is the artifact Linux users install, built against glibc). Name the image tag after the commit
# (gnosis_vpn-client:0.96.3-4c0c0e4c) so a run's provenance (T02-build-provenance) says which build it measured.
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

# Tools sidecar for the regression suite: one per client container, started by `just client-start` with
# `--network container:<client>` so it shares the client's network namespace (the tunnel interface, its routes and
# sysctls) but nothing else. curl, ping, ip and the Python probes run here; the client container stays the upstream
# image untouched (the Alpine release image has neither curl nor python3, and the suite must not need them there).
# gnosis_vpn-ctl, the client log, the deadman and the worker's RSS are still read from the client container itself.
FROM python:3.12-alpine
RUN apk add --no-cache curl iproute2 iputils-ping procps-ng coreutils
CMD ["sleep", "infinity"]

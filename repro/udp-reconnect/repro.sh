#!/usr/bin/env bash
# repro.sh [--load] [DURATION_S] — connect the client and do nothing (default), or run a 1.5 Mbit/s UDP echo flow
# through the tunnel (--load); print one timeline of the client's tunnel-ping / reconnect log lines (and the flow).
# Needs the testenv stack up (see README.md). Standalone: nothing from scripts/suite/.
set -euo pipefail
export LC_ALL=C
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT="${CLIENT:-gnosis_vpn-client}"; DEST="${DEST:-node-0}"; TARGET="${TARGET:-gnosis_vpn-target}"
RATE="${RATE:-1.5}"; SIZE="${SIZE:-1200}"; LOAD=0; DUR=200
for arg in "$@"; do case "$arg" in --load) LOAD=1;; --idle) LOAD=0;; [0-9]*) DUR="$arg";; *) sed -n '2,5p' "$0"; exit 2;; esac; done

ctl() { docker exec "$CLIENT" gnosis_vpn-ctl "$@"; }
log() { printf '%s  %s\n' "$(date -u +%T)" "$*"; }
echo "client: $(ctl --version | head -1)   server: $(docker exec gnosis_vpn-server-0 ./gnosis_vpn-server --version | head -1)   hoprd: $("${HOPRD_BIN:-hoprd}" --version 2>/dev/null | head -1 || echo unknown)"
echo "server wggvpn addresses: $(docker exec gnosis_vpn-server-0 ip -4 -o addr show dev wggvpn | awk '{print $4}' | tr '\n' ' ')   client [connection.ping] address: $(sed -n '/^\[connection.ping\]/,/^\[/p' "${CONFIG_DIR:-/tmp/gnosis_vpn-testenv}/client.toml" 2>/dev/null | sed -nE 's/^address *= *"([^"]+)".*/\1/p' | head -1)"
echo "mode: $([ $LOAD = 1 ] && echo "${RATE} Mbit/s echo" || echo idle) for ${DUR}s"

trap 'ctl disconnect >/dev/null 2>&1 || true; [ -n "${tail_pgid:-}" ] && kill -- -"$tail_pgid" 2>/dev/null || true' EXIT
since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
ctl connect "$DEST" >/dev/null
for _ in $(seq 1 240); do ctl status 2>/dev/null | grep -q '^Connected to' && break; sleep 1; done
ctl status | grep -q '^Connected to' || { echo "connect to $DEST did not complete in 240 s"; exit 1; }
log "session up ($DEST)"

# client log lines that matter, tailed in the background in its own process group so the whole pipeline can be killed
setsid bash -c "docker logs -f --since '$since' '$CLIENT' 2>&1 \
  | sed -E 's/\x1b\[[0-9;]*m//g' \
  | grep --line-buffered -aE 'Ping timed out|tunnel ping exceeded|network link removed|created TUN device|starting tunnel ping' \
  | grep --line-buffered -avE 'received worker response|received response from root|on runner results|incoming response from root' \
  | sed -E -u 's/^([0-9-]+T)?([0-9:]{8})[^ ]* +[A-Z]+ +[^ ]+ +/\2  client: /; s/ conn=Connection.*//; s/incoming from root service line=.*\"request_id\":([0-9]+).*Ping timed out.*/ping \1 timed out/'" &
tail_pgid=$!

if [ $LOAD = 1 ]; then
  target_ip=$(docker inspect "$TARGET" | jq -r '.[0].NetworkSettings.Networks["gnosis-vpn-target"].IPAddress')
  docker cp "$HERE/udp_echo_load.py" "$CLIENT:/tmp/udp_echo_load.py"
  log "load start: ${RATE} Mbit/s, ${SIZE} B datagrams, echo on ${target_ip}:8901"
  docker exec "$CLIENT" python3 /tmp/udp_echo_load.py --host "$target_ip" --port 8901 --rate-mbit "$RATE" --size "$SIZE" --duration "$DUR" | sed -u 's/^/          load:   /'
else
  log "no traffic; idling ${DUR}s"; sleep "$DUR"
fi
sleep 3
log "tunnel-ping timeouts: $(docker logs --since "$since" "$CLIENT" 2>&1 | grep -ac 'TunnelPingResult: Error(Ping timed out)')   reconnects: $(docker logs --since "$since" "$CLIENT" 2>&1 | grep -ac 'tunnel ping exceeded max failures')"

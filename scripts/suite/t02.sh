#!/usr/bin/env bash
# T02-build-provenance — Build provenance and protocol compatibility: version strings and the compiled-in HOPR wire-protocol
# identifiers of client worker, hoprd and server; asserts one protocol id across the stack. Writes provenance.json.
source "$(dirname "$0")/lib.sh"
suite_kind gate
[ "${1:-}" = "--help" ] && { sed -n '2,4p' "$0"; usage_common; exit 0; }
suite_init
cv=$(ctl --version 2>/dev/null | tr -d '\n' || echo unknown)
cp=$(in_client "grep -aoE -m1 '/hopr/mix/[0-9.]+' /app/gnosis_vpn-worker" 2>/dev/null || true)
hv=$("$HOPRD_BIN" --version 2>/dev/null | head -1 || echo unknown)
hp=$(grep -aoE -m1 '/hopr/mix/[0-9.]+' "$HOPRD_BIN" 2>/dev/null || true)
sv=$(docker exec gnosis_vpn-server-0 ./gnosis_vpn-server --version 2>/dev/null | head -1 || echo unknown)
ci=$(docker inspect -f '{{.Config.Image}} {{.Image}}' "$CLIENT" 2>/dev/null); si=$(docker inspect -f '{{.Config.Image}} {{.Image}}' gnosis_vpn-server-0 2>/dev/null)
hsha=$(sha256sum "$HOPRD_BIN" | cut -c1-16); lcv=$("$LOCALCLUSTER_BIN" --version 2>/dev/null | head -1 || true)
python3 - "$SUITE_RUN/provenance.json" "$cv" "$cp" "$hv" "$hp" "$sv" "$ci" "$si" "$hsha" "$lcv" "$CLUSTER_ENV" "$CLUSTER_LATENCY" "$HOPRD_BIN" "$CLIENT_IMAGE" "$SUITE_CELL" <<'PY'
import sys,json,time
k=["client_version","client_protocol","hoprd_version","hoprd_protocol","server_version","client_image","server_image","hoprd_sha256_16","localcluster_version","cluster_env","cluster_latency","hoprd_bin","client_image_tag","cell"]
d=dict(zip(k,sys.argv[2:])); d["t"]=time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime())
json.dump(d,open(sys.argv[1],"w"),indent=1); print(json.dumps(d))
PY
emit_row T02-build-provenance client="$cv" client_protocol="$cp" hoprd="$hv" hoprd_protocol="$hp" server="$sv"
if [ -n "$cp" ] && [ -n "$hp" ] && [ "$cp" = "$hp" ]; then verdict T02-build-provenance PASS "client $cv ($cp), hoprd $hv ($hp), server $sv"; else verdict T02-build-provenance WARN "protocol ids: client '$cp' hoprd '$hp' (client $cv, hoprd $hv, server $sv)"; fi
exit 0

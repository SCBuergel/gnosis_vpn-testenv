#!/bin/bash
# T37-relay-outage A/B of a relay-disconnect trigger (2026-10-10) on fleet t10 (AWS, 10 relays, two drops 30 s apart):
# exclude only (the dropped relay leaves path planning) and the PR alone (both switches off), two pairs each.
# The trigger lives on hoprnet branch test/relay-disconnect-trigger; the exclude+refill pairs ran before this loop
# with both switches at their default (on). B holds the custom builds.
cd ~/gnosis_vpn-testenv
B=${B:-$HOME/gnosis_vpn-testenv-logs/bin}
Q='HOPR_INTERNAL_QUIC_MAX_IDLE_TIMEOUT=2s HOPR_INTERNAL_QUIC_KEEP_ALIVE=500ms'
NL='info,hopr_transport_p2p=debug'
CL='info,hopr_transport_p2p=debug'
run() { # variant pair reup extra-env
  echo "=== $1 pair $2 $(date +%T)"
  multihost/aws_python multihost/relay_outage.py run --name t10 --relays 10 --keep --node-logs $3 \
    --knob T37_STEP_S=30 --knob "T37_ORDER=$2" --node-log "$NL" --client-log "$CL" \
    --node-env "$Q" --client-env "$Q $4" --snapshot ami-016625b23bfdce433 $5 2>&1 \
    | grep --line-buffered -E "^[0-9]{2}:[0-9]{2}:[0-9]{2}|Traceback|Error"
  echo "--- $1 pair $2 done $(date +%T)"; df -h ~ | tail -1
}
A='HOPR_INTERNAL_RELAY_DISCONNECT_REFILL=0'
N='HOPR_INTERNAL_RELAY_DISCONNECT_EXCLUDE=0'
run exclude-only "4 5" --reup "$A" "--client-image gnosis_vpn-client:0.96.4-trial2 --client-image-tar $B/gnosis_vpn-client-0.96.4-trial2.tar.gz"
run exclude-only "6 7" "" "$A" ""
run pr-only "8 9" --reup "$N" ""
run pr-only "0 1" "" "$N" ""
echo "=== loop done $(date +%T)"

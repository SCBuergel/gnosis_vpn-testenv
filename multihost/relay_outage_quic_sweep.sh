#!/bin/bash
# T37-relay-outage QUIC timeout sweep (2026-10-10): steady download 300 s, then relay 0 dropped for 300 s, five
# idle/keep-alive settings on client and exit, first on the plain network, then with netem on every machine.
# Fleet qsw (AWS, 5 relays). B holds the custom builds; the sweep ran against a QUIC build that still read the
# _MS variables, the names below are the ones hopr-impls#48 settled on (humantime values).
cd ~/gnosis_vpn-testenv
B=${B:-$HOME/gnosis_vpn-testenv-logs/bin}
NL='info,hopr_transport_p2p=debug'
CL='warn,gnosis_vpn_root=debug,gnosis_vpn_lib=debug,gnosis_vpn_worker=debug,hopr_transport_p2p=debug'
SETTINGS=("default:" "2s:HOPR_INTERNAL_QUIC_MAX_IDLE_TIMEOUT=2s HOPR_INTERNAL_QUIC_KEEP_ALIVE=500ms" "1s:HOPR_INTERNAL_QUIC_MAX_IDLE_TIMEOUT=1s HOPR_INTERNAL_QUIC_KEEP_ALIVE=250ms" "750ms:HOPR_INTERNAL_QUIC_MAX_IDLE_TIMEOUT=750ms HOPR_INTERNAL_QUIC_KEEP_ALIVE=190ms" "500ms:HOPR_INTERNAL_QUIC_MAX_IDLE_TIMEOUT=500ms HOPR_INTERNAL_QUIC_KEEP_ALIVE=125ms")
netem() {  # $1 = on|off, on every fleet machine but control
  multihost/aws_python multihost/aws_fleet.py list --name qsw 2>/dev/null | grep -E "client-|exit-|relay-" | grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' | sort -u | while read ip; do
    case "$1" in
      on) c='dev=$(ip -o route get 1.1.1.1 | sed -E "s/.* dev ([^ ]+).*/\1/"); tc qdisc replace dev $dev root netem delay 25ms 10ms distribution normal loss 0.25% && echo "$(hostname) $dev netem on"';;
      off) c='dev=$(ip -o route get 1.1.1.1 | sed -E "s/.* dev ([^ ]+).*/\1/"); tc qdisc del dev $dev root 2>/dev/null; echo "$(hostname) netem off"';;
    esac
    ssh -n -i ~/.ssh/do_testenv_fleet_ed25519 -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o BatchMode=yes -o LogLevel=ERROR -o ConnectTimeout=20 root@$ip "$c"
  done
}
first=1
for net in plain netem; do
  [ $net = netem ] && netem on
  for s in "${SETTINGS[@]}"; do
    name=${s%%:*}; Q=${s#*:}
    echo "=== $net $name $(date +%T)"
    extra=""; [ $first = 1 ] && extra="--client-image gnosis_vpn-client:0.96.4-quic --client-image-tar $B/gnosis_vpn-client-0.96.4-quic.tar.gz --exit-hoprd $B/hoprd-4.1.3-exitfix3 --localcluster $B/hoprd-localcluster-4.1.3-max16-funder-preannounce"
    reup="--reup"; [ $first = 1 ] && reup=""
    first=0
    multihost/aws_python multihost/relay_outage.py run --name qsw --relays 5 --keep $reup --node-logs \
      --knob T37_STEP_S=300 --knob T37_ORDER=0 --node-log "$NL" --client-log "$CL" \
      --node-env "$Q" --client-env "$Q" --snapshot ami-016625b23bfdce433 $extra 2>&1 \
      | grep -E "^[0-9]{2}:[0-9]{2}:[0-9]{2}|Traceback|Error"
    echo "--- $net $name done $(date +%T)"
  done
done
netem off
echo "=== sweep done $(date +%T)"

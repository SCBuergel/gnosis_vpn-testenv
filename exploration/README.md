# Exploration: can the Gnosis VPN tunnel be broken?

This folder is a scratch harness, not part of the regression suite. It is git-excluded (`.git/info/exclude`) and must not be committed. It reuses the suite's helpers (`scripts/suite/lib.sh`) and the probes already mounted at `/suite/probes` inside the client, but writes only to `exploration/runs/` and is never listed in `run.sh`.

## The question

The suite proves the tunnel is well-behaved on the happy path. This asks the opposite: is there an input that breaks the tunnel itself, rather than merely overwhelming the path? The two are different verdicts:

- **BROKE** — the client tore the tunnel down (reconnect, interface recreated, worker restart), the session dropped to Disconnected, or frames were lost to reassembly/decapsulation errors. A transport failure.
- **OVERLOAD** — packets were lost or delayed but the tunnel stayed up and logged no transport error. Expected when you push past capacity. Not a finding.
- **CLEAN** — delivered within tolerance, tunnel intact.

The liveness-ping artifact (client pings `10.128.0.1`, testenv server holds only `10.129.0.1`, so every periodic ping times out and the watchdog reconnects every ~85 s) is assumed fixed here by the `SERVER_PING_ALIAS` stopgap. Every scenario prints `ping_timeouts` next to `reconnects`; a reconnect with about three ping timeouts each **at idle** is that artifact, not a finding, and the classifier flags it.

## Scenarios

Each is one hypothesis about how the transport, not the path, could fail.

| scenario | hypothesis |
| --- | --- |
| `pure-download` | a sustained server→client stream is the maximum return-path (SURB) demand; does the client tear down when it cannot supply SURBs fast enough, rather than just slowing? |
| `idle-resume` | after the tunnel idles past SURB expiry, does the first transfer resume, or does the tunnel reconnect to recover? (a real weakness now that the ping is healthy) |
| `mtu-frag` | datagrams larger than the tunnel MTU force fragmentation and reassembly; does reassembly fail or the tunnel stall, versus cleanly dropping oversize packets? |
| `reorder` | `tc netem` reorders and duplicates packets between relays; does the decapsulator stall (`DecapStalled`) or the client reconnect? |
| `churn` | rapid connect/transfer/disconnect cycles; does state leak (channel `PendingToClose`, wedged worker) so a later cycle fails after early ones passed? |
| `burst-idle` | repeated burst+idle on one session; does the SURB ramp/decay accumulate into a reconnect? |
| `bidir` | simultaneous up+down at a moderate rate; does contention on one session collapse a direction or reconnect? |
| `soak` | moderate sustained flow for 30 min; re-tests the old unattributed "soak dead at +21 min" with the liveness ping healthy. |

## Running (only when the stack is free — never while the suite runs)

```sh
cd /root/testenv/gnosis_vpn-testenv        # on the suite host
exploration/explore.sh pure-download
exploration/explore.sh mtu-frag SIZES="2000 4000 8000"
exploration/explore.sh soak DUR=1800
```

Results land in `exploration/runs/<stamp>-<scenario>/` as `results.tsv`, `explore.log`, per-arm probe JSON and saved client logs.

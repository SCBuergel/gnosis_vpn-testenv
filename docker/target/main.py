#!/usr/bin/env python3
"""Run the four target services in one process; exit when any of them dies so the container restarts cleanly.
The services are plain modules (callecho, streamsrv, callsrv, speedtarget) that the suite's self-tests also start
on loopback without a container; the container exists only to place them on the far side of the exit."""
import os
import sys
import threading

import callecho
import callsrv
import speedtarget
import streamsrv

SERVICES = {
    "speedtarget": lambda: speedtarget.serve(int(os.environ.get("HTTP_PORT", "8899"))),
    "callecho": lambda: callecho.serve(int(os.environ.get("ECHO_PORT", "8901"))),
    "streamsrv": lambda: streamsrv.serve(int(os.environ.get("STREAM_PORT", "8902"))),
    "callsrv": lambda: callsrv.serve(int(os.environ.get("CALL_PORT", "8903")), os.environ.get("CALLSRV_LOGDIR", "/root/callsrv")),
}


def main():
    died = threading.Event()

    def run(name, fn):
        try:
            fn()
        except BaseException as e:   # noqa: BLE001 - any exit of a service ends the container
            print(f"{name} died: {e!r}", flush=True)
        died.set()

    for name, fn in SERVICES.items():
        threading.Thread(target=run, args=(name, fn), daemon=True).start()
    died.wait()
    sys.exit(1)


if __name__ == "__main__":
    main()

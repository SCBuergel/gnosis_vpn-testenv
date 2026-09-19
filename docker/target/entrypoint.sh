#!/bin/bash
# run all four target services; exit when any of them dies so the container restarts cleanly
set -u
PORT=8899 python3 -u speedtarget.py &
PORT=8901 python3 -u callecho.py &
PORT=8902 python3 -u streamsrv.py &
python3 -u callsrv.py &
wait -n
exit 1

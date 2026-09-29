#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/common.sh"

: "${REPO_DIR:?}"
: "${METRICS_DATA_DIR:?}"

start() {
    local running
    running=$(pgrep -f "otelcol --config" 2>/dev/null || true)
    if [ -n "${running}" ]; then
        echo "Metrics found (PID ${running}) — skipping start"
        echo "  OTLP HTTP: 127.0.0.1:4318 | PromQL UI: http://localhost:8428"
        return 0
    fi

    mkdir -p "${METRICS_DATA_DIR}"

    setsid nohup otelcol --config "${REPO_DIR}/configs/otelcol.yaml" >/tmp/hopr-otelcol.log 2>&1 &
    setsid nohup victoria-metrics \
        -storageDataPath "${METRICS_DATA_DIR}" \
        -httpListenAddr "127.0.0.1:8428" \
        >/tmp/hopr-victoriametrics.log 2>&1 &

    echo "Started metrics — OTLP HTTP: 127.0.0.1:4318 | PromQL UI: http://localhost:8428"
}

stop() {
    pkill -f "otelcol --config" 2>/dev/null || true
    pkill -f "victoria-metrics" 2>/dev/null || true
    echo "Metrics stopped"
}

case "${1:-}" in
start) start ;;
stop) stop ;;
*) die "usage: metrics.sh start|stop" ;;
esac

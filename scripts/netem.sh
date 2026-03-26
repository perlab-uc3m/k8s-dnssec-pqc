#!/usr/bin/env bash
# netem.sh — Apply or remove tc-netem network emulation on Kind worker node.
#
# Adds a one-way delay and optional bandwidth limit on the worker container's
# eth0 so that every packet leaving CoreDNS (DNS responses) is affected.
# A 50 ms one-way delay ≈ 100 ms RTT, which makes the TCP handshake cost
# visible: UDP queries pay ~1 RTT, TCP queries pay ~3 RTT.
# A bandwidth limit (e.g. 500 kbit) makes response size a continuous
# performance variable: every extra byte of signature costs measurable time.
#
# Usage:
#   netem.sh apply  <delay_ms> [jitter_ms] [rate_kbps]
#       e.g. netem.sh apply 50 0 500    # 50ms delay + 500 kbps limit
#   netem.sh remove                      # remove all qdisc rules
#   netem.sh status                      # show current tc rules
#
# Requires: iproute2 (tc) inside the Kind container (already present in
# kindest/node images).  Needs NET_ADMIN capability (Kind grants it).
set -euo pipefail

CLUSTER_NAME="${KIND_CLUSTER_NAME:-pqc-dnssec-bench}"
WORKER="${CLUSTER_NAME}-worker"

log() { echo "[netem] $*"; }

_docker_exec() {
    docker exec "$WORKER" "$@"
}

case "${1:-status}" in
apply)
    DELAY_MS="${2:?Usage: netem.sh apply <delay_ms> [jitter_ms] [rate_kbps]}"
    JITTER_MS="${3:-0}"
    RATE_KBPS="${4:-0}"

    # Remove existing qdisc first (ignore errors if none exists)
    _docker_exec tc qdisc del dev eth0 root 2>/dev/null || true

    HAS_DELAY=false
    HAS_RATE=false
    [ "$DELAY_MS" -gt 0 ] 2>/dev/null && HAS_DELAY=true
    [ "$RATE_KBPS" -gt 0 ] 2>/dev/null && HAS_RATE=true

    LOG_MSG=""

    if $HAS_DELAY && $HAS_RATE; then
        # Stacked: netem (delay/jitter) as root, tbf (rate) as child.
        # netem's built-in `rate` param only adds inter-packet delay;
        # tbf enforces a real token-bucket bandwidth cap.
        NETEM_ARGS="delay ${DELAY_MS}ms"
        if [ "$JITTER_MS" -gt 0 ] 2>/dev/null; then
            NETEM_ARGS="${NETEM_ARGS} ${JITTER_MS}ms"
        fi
        _docker_exec tc qdisc add dev eth0 root handle 1: netem $NETEM_ARGS

        # tbf burst: 100 ms of traffic, minimum 1 MTU (1540 B)
        RATE_BPS=$(( RATE_KBPS * 1000 / 8 ))
        BURST=$(( RATE_BPS / 10 ))
        [ "$BURST" -lt 1540 ] && BURST=1540
        _docker_exec tc qdisc add dev eth0 parent 1:1 handle 10: \
            tbf rate ${RATE_KBPS}kbit burst ${BURST} latency 50ms

        LOG_MSG="${DELAY_MS}ms delay + ${RATE_KBPS} kbps rate limit (netem+tbf)"

    elif $HAS_RATE; then
        # Rate-only: tbf as root qdisc
        RATE_BPS=$(( RATE_KBPS * 1000 / 8 ))
        BURST=$(( RATE_BPS / 10 ))
        [ "$BURST" -lt 1540 ] && BURST=1540
        _docker_exec tc qdisc add dev eth0 root \
            tbf rate ${RATE_KBPS}kbit burst ${BURST} latency 50ms

        LOG_MSG="${RATE_KBPS} kbps rate limit (tbf)"

    elif $HAS_DELAY; then
        # Delay-only: netem as root qdisc
        NETEM_ARGS="delay ${DELAY_MS}ms"
        if [ "$JITTER_MS" -gt 0 ] 2>/dev/null; then
            NETEM_ARGS="${NETEM_ARGS} ${JITTER_MS}ms"
        fi
        _docker_exec tc qdisc add dev eth0 root netem $NETEM_ARGS

        LOG_MSG="${DELAY_MS}ms delay"
    else
        log "No delay or rate specified, nothing to apply"
        exit 0
    fi

    if [ "$JITTER_MS" -gt 0 ] 2>/dev/null; then
        LOG_MSG="${LOG_MSG} ± ${JITTER_MS}ms jitter"
    fi
    log "Applied ${LOG_MSG} on ${WORKER}:eth0"
    ;;
remove)
    _docker_exec tc qdisc del dev eth0 root 2>/dev/null || true
    log "Removed netem rules from ${WORKER}:eth0"
    ;;
status)
    log "Current tc rules on ${WORKER}:eth0:"
    _docker_exec tc qdisc show dev eth0 || true
    ;;
*)
    echo "Usage: netem.sh {apply|remove|status} [delay_ms] [jitter_ms]" >&2
    exit 1
    ;;
esac

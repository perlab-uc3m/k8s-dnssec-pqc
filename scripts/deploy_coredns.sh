#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="${REPO_DIR}/build"
K8S_DIR="${REPO_DIR}/k8s"
KEYS_DIR="${BUILD_DIR}/keys"

ALGORITHM="${1:?Usage: deploy_coredns.sh <algorithm_name> <algorithm_id> [cache_ttl] [type] [sig_cache_cap] [sim_delay] [sim_stddev]}"
ALGORITHM_ID="${2:?Usage: deploy_coredns.sh <algorithm_name> <algorithm_id> [cache_ttl] [type] [sig_cache_cap] [sim_delay] [sim_stddev]}"
DOMAIN="cluster.local"
CACHE_TTL="${3:-5}"
ALGO_TYPE="${4:-pqc}"
SIG_CACHE_CAP="${5:-}"  # empty = plugin default (10000); "0" = disabled
SIM_DELAY="${6:-}"      # e.g. "50ms" — Go duration string
SIM_STDDEV="${7:-}"     # e.g. "5ms"  — Go duration string

log() { echo "[deploy] $*"; }

# --- Generate keys ---
# --- Generate keys (skip for baseline) ---
if [ "$ALGO_TYPE" = "baseline" ]; then
    log "Baseline mode: no DNSSEC signing"
    SECRET_KEY_NAME=""
else
    mkdir -p "$KEYS_DIR"
    # Clean both PQC (K<domain>+*) and classical (K<domain>.+*) patterns
    rm -f "${KEYS_DIR}"/K${DOMAIN}+*
    rm -f "${KEYS_DIR}"/K${DOMAIN}.*
    log "Generating keys for ${ALGORITHM} (id=${ALGORITHM_ID}, type=${ALGO_TYPE})"
    cd "$KEYS_DIR"

    if [ "$ALGO_TYPE" = "classical" ]; then
        # Use BIND's dnssec-keygen for classical algorithms
        dnssec-keygen -a "$ALGORITHM" -n ZONE "$DOMAIN" 2>/dev/null
    else
        # Use PQC keygen
        "${BUILD_DIR}/keygen" -algorithm "$ALGORITHM" -number "$ALGORITHM_ID" -domain "$DOMAIN"
    fi

    # Find key files — classical uses K<domain>.+NNN+ID, PQC uses K<domain>+NNN+ID
    KEY_FILE=$(find "${KEYS_DIR}" -maxdepth 1 \( -name "K${DOMAIN}+*.key" -o -name "K${DOMAIN}.+*.key" \) -print -quit)
    PRIVATE_FILE=$(find "${KEYS_DIR}" -maxdepth 1 \( -name "K${DOMAIN}+*.private" -o -name "K${DOMAIN}.+*.private" \) -print -quit)

    if [ -z "$KEY_FILE" ] || [ -z "$PRIVATE_FILE" ]; then
        echo "ERROR: Key generation failed" >&2
        exit 1
    fi

    KEY_BASENAME=$(basename "$KEY_FILE" .key)
    # Sanitize for k8s secret key names (replace + with -)
    SECRET_KEY_NAME=$(echo "$KEY_BASENAME" | tr '+' '-')
fi

# --- Build Corefile based on algorithm type ---
SIG_CACHE_LINE=""
if [ -n "$SIG_CACHE_CAP" ]; then
    SIG_CACHE_LINE="        cache_capacity ${SIG_CACHE_CAP}"
fi

SIM_DELAY_LINE=""
if [ -n "$SIM_DELAY" ]; then
    SIM_DELAY_LINE="        simulated_delay ${SIM_DELAY}"
fi

SIM_STDDEV_LINE=""
if [ -n "$SIM_STDDEV" ]; then
    SIM_STDDEV_LINE="        simulated_stddev ${SIM_STDDEV}"
fi

if [ "$ALGO_TYPE" = "baseline" ]; then
    DNSSEC_BLOCK=""
else
    DNSSEC_BLOCK="    dnssec_pqc {
        key file /etc/coredns/keys/${SECRET_KEY_NAME}
${SIG_CACHE_LINE:+$SIG_CACHE_LINE
}${SIM_DELAY_LINE:+$SIM_DELAY_LINE
}${SIM_STDDEV_LINE:+$SIM_STDDEV_LINE
}    }"
fi

COREFILE=$(cat <<EOF
${DOMAIN}:53 {
    kubernetes ${DOMAIN} in-addr.arpa ip6.arpa {
        pods insecure
        fallthrough in-addr.arpa ip6.arpa
    }
${DNSSEC_BLOCK:+${DNSSEC_BLOCK}
}$([ "${CACHE_TTL}" -gt 0 ] 2>/dev/null && echo "    cache ${CACHE_TTL}" || true)
    log
    errors
    prometheus :9153
}
.:53 {
    forward . /etc/resolv.conf
    cache 30
    log
    errors
}
EOF
)

# --- Create keys secret (skip for baseline) ---
kubectl -n kube-system delete secret coredns-pqc-keys --ignore-not-found
if [ -n "$SECRET_KEY_NAME" ]; then
    kubectl -n kube-system create secret generic coredns-pqc-keys \
        --from-file="${SECRET_KEY_NAME}.key=${KEY_FILE}" \
        --from-file="${SECRET_KEY_NAME}.private=${PRIVATE_FILE}"
else
    # Baseline: create empty secret so volume mount still works
    kubectl -n kube-system create secret generic coredns-pqc-keys
fi

# --- Apply Corefile ConfigMap ---
kubectl -n kube-system delete configmap coredns-pqc-config --ignore-not-found
kubectl -n kube-system create configmap coredns-pqc-config \
    --from-literal=Corefile="$COREFILE"

# --- Deploy CoreDNS PQC ---
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: coredns-pqc
  namespace: kube-system
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: coredns-pqc
rules:
  - apiGroups: [""]
    resources: ["services", "namespaces", "endpoints", "pods"]
    verbs: ["list", "watch", "get"]
  - apiGroups: ["discovery.k8s.io"]
    resources: ["endpointslices"]
    verbs: ["list", "watch", "get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: coredns-pqc
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: coredns-pqc
subjects:
  - kind: ServiceAccount
    name: coredns-pqc
    namespace: kube-system
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: coredns-pqc
  namespace: kube-system
  labels:
    app: coredns-pqc
spec:
  replicas: 1
  selector:
    matchLabels:
      app: coredns-pqc
  template:
    metadata:
      labels:
        app: coredns-pqc
    spec:
      serviceAccountName: coredns-pqc
      containers:
        - name: coredns
          image: coredns-pqc:latest
          imagePullPolicy: Never
          args: ["-conf", "/etc/coredns/Corefile"]
          ports:
            - containerPort: 53
              protocol: UDP
            - containerPort: 53
              protocol: TCP
            - containerPort: 9153
              protocol: TCP
          resources:
            requests:
              memory: "256Mi"
              cpu: "250m"
            limits:
              memory: "1Gi"
              cpu: "2000m"
          volumeMounts:
            - name: config
              mountPath: /etc/coredns
              readOnly: true
            - name: keys
              mountPath: /etc/coredns/keys
              readOnly: true
      volumes:
        - name: config
          configMap:
            name: coredns-pqc-config
        - name: keys
          secret:
            secretName: coredns-pqc-keys
---
apiVersion: v1
kind: Service
metadata:
  name: coredns-pqc
  namespace: kube-system
spec:
  type: NodePort
  selector:
    app: coredns-pqc
  ports:
    - name: dns
      port: 53
      targetPort: 53
      protocol: UDP
      nodePort: 30053
    - name: dns-tcp
      port: 53
      targetPort: 53
      protocol: TCP
      nodePort: 30054
    - name: metrics
      port: 9153
      targetPort: 9153
      protocol: TCP
      nodePort: 30153
EOF

log "Restarting CoreDNS PQC pod to pick up new config/keys..."
kubectl -n kube-system rollout restart deployment/coredns-pqc
log "Waiting for CoreDNS PQC pod to be ready..."
kubectl -n kube-system rollout status deployment/coredns-pqc --timeout=180s

COREDNS_IP=$(kubectl -n kube-system get svc coredns-pqc -o jsonpath='{.spec.clusterIP}')
log "CoreDNS PQC deployed at ${COREDNS_IP} with algorithm ${ALGORITHM}"
echo "$COREDNS_IP"

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="${REPO_DIR}/build"
K8S_DIR="${REPO_DIR}/k8s"
CLUSTER_NAME="pqc-dnssec-bench"
IMAGE_NAME="coredns-pqc:latest"
KIND_VERSION="v0.27.0"

log() { echo "[setup] $*"; }

# --- Install kind if not present ---
if ! command -v kind &>/dev/null; then
    log "Installing kind ${KIND_VERSION}"
    curl -Lo /tmp/kind "https://kind.sigs.k8s.io/dl/${KIND_VERSION}/kind-linux-amd64"
    chmod +x /tmp/kind
    sudo mv /tmp/kind /usr/local/bin/kind
    log "kind installed"
fi

# --- Create cluster ---
if kind get clusters 2>/dev/null | grep -q "$CLUSTER_NAME"; then
    log "Cluster ${CLUSTER_NAME} already exists"
else
    log "Creating kind cluster: ${CLUSTER_NAME}"
    kind create cluster --config "${K8S_DIR}/kind-config.yaml"
fi

# --- Apply memory limits to Kind containers ---
# Prevents the benchmark from consuming all host RAM and crashing the system.
# Control plane (etcd + apiserver) gets more; worker gets less.
CONTROL_PLANE_MEM="${KIND_CP_MEMORY:-10g}"
WORKER_MEM="${KIND_WORKER_MEMORY:-4g}"
log "Applying memory limits: control-plane=${CONTROL_PLANE_MEM}, worker=${WORKER_MEM}"
docker update --memory="${CONTROL_PLANE_MEM}" --memory-swap="${CONTROL_PLANE_MEM}" "${CLUSTER_NAME}-control-plane" || true
docker update --memory="${WORKER_MEM}" --memory-swap="${WORKER_MEM}" "${CLUSTER_NAME}-worker" || true

# --- Load CoreDNS PQC image into kind ---
log "Loading ${IMAGE_NAME} into kind"
kind load docker-image "$IMAGE_NAME" --name "$CLUSTER_NAME"

# --- Create benchmark namespace ---
kubectl create namespace bench --dry-run=client -o yaml | kubectl apply -f -

log "Cluster ready (memory-limited). Use deploy_coredns.sh <algorithm> to deploy CoreDNS with a specific PQC algorithm."

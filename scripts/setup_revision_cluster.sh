#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
kind_bin="$repo_dir/build/tools/kind"
kubectl_bin="$repo_dir/build/tools/kubectl"
cluster=pqc-dnssec-revision
context=kind-pqc-dnssec-revision
test -x "$kind_bin"
test -x "$kubectl_bin"
test -f "$repo_dir/build/image-id.txt"
if ! "$kind_bin" get clusters | rg -qx "$cluster"; then
    "$kind_bin" create cluster --config "$repo_dir/k8s/kind-laptop.yaml"
fi
docker update --memory="${KIND_MEMORY_LIMIT:-4g}" \
    --memory-swap="${KIND_MEMORY_LIMIT:-4g}" "$cluster-control-plane"
"$kind_bin" load docker-image coredns-pqc:revision --name "$cluster"
"$kubectl_bin" --context "$context" create namespace bench \
    --dry-run=client -o yaml | "$kubectl_bin" --context "$context" apply -f -
"$kubectl_bin" --context "$context" get nodes -o wide

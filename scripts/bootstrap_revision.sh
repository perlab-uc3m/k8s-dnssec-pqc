#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$repo_dir/build/tools"
if [ ! -x "$repo_dir/build/venv/bin/python" ]; then
    python3 -m venv "$repo_dir/build/venv"
    "$repo_dir/build/venv/bin/python" -m pip install -r "$repo_dir/config/revision-python.lock"
fi
fetch_tool() {
    local name="$1" url="$2" sha="$3"
    local file="$repo_dir/build/tools/$name"
    if [ ! -f "$file" ]; then
        curl --fail --location --retry 3 --output "$file.tmp" "$url"
        mv "$file.tmp" "$file"
        chmod +x "$file"
    fi
    printf '%s  %s\n' "$sha" "$file" | sha256sum --check --status
}
fetch_tool kind \
    https://kind.sigs.k8s.io/dl/v0.27.0/kind-linux-amd64 \
    a6875aaea358acf0ac07786b1a6755d08fd640f4c79b7a2e46681cc13f49a04b
fetch_tool kubectl \
    https://dl.k8s.io/release/v1.32.0/bin/linux/amd64/kubectl \
    646d58f6d98ee670a71d9cdffbf6625aeea2849d567f214bc43a35f8ccb7bf70

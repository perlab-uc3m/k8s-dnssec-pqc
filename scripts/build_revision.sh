#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="$repo_dir/build"
mkdir -p "$build_dir"
clone_exact() {
    local url="$1" branch="$2" sha="$3" dest="$4"
    if [ ! -d "$dest/.git" ]; then
        git init "$dest"
        git -C "$dest" remote add origin "$url"
        git -C "$dest" fetch --depth 1 origin "$sha"
        git -C "$dest" checkout --detach FETCH_HEAD
    fi
    test "$(git -C "$dest" rev-parse HEAD)" = "$sha"
}
clone_exact https://github.com/open-quantum-safe/liboqs.git 0.14.0-rc1 \
    a05831ac106619cdbcffb22b16efe87f70fb7770 "$build_dir/liboqs"
clone_exact https://github.com/qursa-uc3m/dnssec_pqc_plugin.git no-cache \
    9b852e908175d4d6c34641365c29485635eb07eb "$build_dir/dnssec_pqc_plugin"
clone_exact https://github.com/fj-blanco/coredns.git no-cache \
    275f84b4c16eca48f6fff3cbc517f99a6716d333 "$build_dir/coredns"
clone_exact https://github.com/qursa-uc3m/dns.git master \
    30d7ff0208cbb3ae3946fb6991c1e54da304dfbb "$build_dir/dns"
if git -C "$build_dir/dns" apply --check "$repo_dir/patches/dns-pqc-verify.patch" 2>/dev/null; then
    git -C "$build_dir/dns" apply "$repo_dir/patches/dns-pqc-verify.patch"
elif ! git -C "$build_dir/dns" apply --reverse --check "$repo_dir/patches/dns-pqc-verify.patch" 2>/dev/null; then
    echo "DNS fork has unexpected changes; refusing to build" >&2
    exit 1
fi
if git -C "$build_dir/dnssec_pqc_plugin" apply --check "$repo_dir/patches/plugin-signing-failure.patch" 2>/dev/null; then
    git -C "$build_dir/dnssec_pqc_plugin" apply "$repo_dir/patches/plugin-signing-failure.patch"
elif ! git -C "$build_dir/dnssec_pqc_plugin" apply --reverse --check "$repo_dir/patches/plugin-signing-failure.patch" 2>/dev/null; then
    echo "Plugin fork has unexpected changes; refusing to build" >&2
    exit 1
fi
# Default to isolated response records. The baseline option reproduces the
# archived runs whose TTL comparison exposed shared signature mutation.
ownership_patch="$repo_dir/patches/plugin-signature-ownership.patch"
case "${BENCH_SIGNATURE_OWNERSHIP_FIX:-1}" in
    1)
        if git -C "$build_dir/dnssec_pqc_plugin" apply --check "$ownership_patch" 2>/dev/null; then
            git -C "$build_dir/dnssec_pqc_plugin" apply "$ownership_patch"
        else
            git -C "$build_dir/dnssec_pqc_plugin" apply --reverse --check "$ownership_patch"
        fi
        ;;
    0)
        if git -C "$build_dir/dnssec_pqc_plugin" apply --reverse --check "$ownership_patch" 2>/dev/null; then
            git -C "$build_dir/dnssec_pqc_plugin" apply --reverse "$ownership_patch"
        else
            git -C "$build_dir/dnssec_pqc_plugin" apply --check "$ownership_patch"
        fi
        ;;
    *) echo "BENCH_SIGNATURE_OWNERSHIP_FIX must be 0 or 1" >&2; exit 1 ;;
esac
eviction_patch="$repo_dir/patches/plugin-cache-evictions.patch"
if git -C "$build_dir/dnssec_pqc_plugin" apply --check "$eviction_patch" 2>/dev/null; then
    git -C "$build_dir/dnssec_pqc_plugin" apply "$eviction_patch"
else
    git -C "$build_dir/dnssec_pqc_plugin" apply --reverse --check "$eviction_patch"
fi
# Every signature scheme of the submitted manuscript. Changing this list rebuilds liboqs.
oqs_algorithms='SIG_ml_dsa_44;SIG_ml_dsa_65;SIG_ml_dsa_87;SIG_falcon_512;SIG_falcon_1024;SIG_sphincs_sha2_128s_simple;SIG_mayo_1;SIG_mayo_3;SIG_snova_SNOVA_24_5_4'
if [ ! -f "$build_dir/local/lib/liboqs.so" ] || \
    [ "$(cat "$build_dir/local/liboqs-algorithms.txt" 2>/dev/null)" != "$oqs_algorithms" ]; then
    rm -rf "$build_dir/liboqs/out" "$build_dir/local"
    cmake -S "$build_dir/liboqs" -B "$build_dir/liboqs/out" -G Ninja \
        -DCMAKE_INSTALL_PREFIX="$build_dir/local" \
        -DBUILD_SHARED_LIBS=ON -DOQS_BUILD_ONLY_LIB=ON -DOQS_DIST_BUILD=ON \
        -DCMAKE_BUILD_TYPE=Release \
        "-DOQS_MINIMAL_BUILD=$oqs_algorithms"
    cmake --build "$build_dir/liboqs/out" --parallel "${BUILD_JOBS:-2}"
    cmake --install "$build_dir/liboqs/out"
    printf '%s' "$oqs_algorithms" > "$build_dir/local/liboqs-algorithms.txt"
fi
mkdir -p "$build_dir/local/lib/pkgconfig"
cat > "$build_dir/local/lib/pkgconfig/liboqs-go.pc" <<'PKGCONFIG'
Name: liboqs-go
Description: C dependency for liboqs-go bindings
Version: 0.14.0-rc1
Requires: liboqs
PKGCONFIG
export GOTOOLCHAIN=go1.26.0
export GOFLAGS="-p=${BUILD_JOBS:-2} -mod=mod"
export GOMAXPROCS="${BUILD_JOBS:-2}"
export GOMEMLIMIT="${BUILD_GO_MEMORY:-1GiB}"
export PKG_CONFIG_PATH="$build_dir/local/lib/pkgconfig"
export LD_LIBRARY_PATH="$build_dir/local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
(
    cd "$build_dir/coredns"
    cat > plugin.cfg <<'PLUGINS'
root:root
metadata:metadata
prometheus:metrics
errors:errors
log:log
cache:cache
dnssec_pqc:github.com/qursa-uc3m/dnssec_pqc_plugin
kubernetes:kubernetes
forward:forward
PLUGINS
    go mod edit \
        -replace "github.com/miekg/dns=$build_dir/dns" \
        -replace "github.com/qursa-uc3m/dnssec_pqc_plugin=$build_dir/dnssec_pqc_plugin" \
        -require "github.com/qursa-uc3m/dnssec_pqc_plugin@v0.1.1"
    go run directives_generate.go
    CGO_ENABLED=1 go test github.com/qursa-uc3m/dnssec_pqc_plugin -run 'TestSigningFailureReturnsSERVFAIL|TestSignatureResponseOwnership|TestSignatureCacheEvictionCounter' -count=1
    CGO_ENABLED=1 go build -a -o "$build_dir/coredns-pqc"
)
cd "$build_dir/dns"
CGO_ENABLED=1 go test . -run TestPQCVerifyRepeatedAndTampered -count=1
CGO_ENABLED=1 go build -o "$build_dir/verify-dns" "$repo_dir/tools/verify/main.go"
CGO_ENABLED=1 go build -o "$build_dir/keygen-ed" "$repo_dir/tools/keygen_ed/main.go"
CGO_ENABLED=1 go build -o "$build_dir/check-signers" "$repo_dir/tools/check_signers/main.go"
"$build_dir/check-signers" "$build_dir/keygen-ed" > "$build_dir/signer-checks.txt"
cat "$build_dir/signer-checks.txt"
cd "$build_dir/dnssec_pqc_plugin/keygen"
CGO_ENABLED=1 go build -o "$build_dir/keygen"
mkdir -p "$build_dir/image"
cp "$build_dir/coredns-pqc" "$build_dir/image/coredns"
cp -L "$build_dir/local/lib/liboqs.so.8" "$build_dir/image/liboqs.so.8"
cat > "$build_dir/image/Dockerfile" <<'DOCKER'
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
RUN apt-get update && apt-get install -y --no-install-recommends libssl3 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY coredns /usr/local/bin/coredns
COPY liboqs.so.8 /usr/local/lib/liboqs.so.8
RUN ldconfig
ENTRYPOINT ["/usr/local/bin/coredns"]
DOCKER
docker build -t coredns-pqc:revision "$build_dir/image"
go version -m "$build_dir/coredns-pqc" > "$build_dir/go-build-info.txt"
docker image inspect coredns-pqc:revision --format '{{.Id}}' > "$build_dir/image-id.txt"
echo "Built coredns-pqc:revision; image ID $(cat "$build_dir/image-id.txt")"

"$build_dir/venv/bin/python" - "$repo_dir" "${BENCH_SIGNATURE_OWNERSHIP_FIX:-1}" <<'PYMANIFEST'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
paths = sorted((root / "patches").glob("*.patch")) + sorted((root / "tools").rglob("*.go"))
paths += [root / "scripts/build_revision.sh"]
artifacts = ["coredns-pqc", "verify-dns", "keygen", "keygen-ed", "local/lib/liboqs.so.8", "go-build-info.txt", "signer-checks.txt"]
record = {"artifacts": {"build/" + name: hashlib.sha256((root / "build" / name).read_bytes()).hexdigest() for name in artifacts},
          "signature_ownership_fix": sys.argv[2] == "1",
          "image_id": (root / "build/image-id.txt").read_text().strip(),
          "inputs": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
          "binary_sha256": hashlib.sha256((root / "build/coredns-pqc").read_bytes()).hexdigest()}
(root / "build/source-manifest.json").write_text(json.dumps(record, indent=2) + "\n")
PYMANIFEST

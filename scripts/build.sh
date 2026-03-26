#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
BUILD_DIR="${REPO_DIR}/build"

# Versions (single source of truth: also in config/benchmark.yaml)
LIBOQS_VERSION="0.14.0-rc1"
COREDNS_VERSION="no-cache"
COREDNS_REPO="https://github.com/fj-blanco/coredns.git"
GO_VERSION="1.23.11"
PLUGIN_REPO="https://github.com/qursa-uc3m/dnssec_pqc_plugin.git"
PLUGIN_BRANCH="no-cache"
PLUGIN_TAG="v0.1.1"
DNS_REPO="github.com/qursa-uc3m/dns"
IMAGE_NAME="coredns-pqc"
IMAGE_TAG="latest"

log() { echo "[build] $*"; }

mkdir -p "$BUILD_DIR"

# --- Step 1: Install liboqs if not present ---
install_liboqs() {
    local installed
    installed=$(pkg-config --modversion liboqs 2>/dev/null || echo "none")
    if [ "$installed" = "${LIBOQS_VERSION}" ]; then
        log "liboqs ${LIBOQS_VERSION} already installed"
        return
    fi
    log "liboqs installed version: ${installed} (need ${LIBOQS_VERSION})"
    log "Installing liboqs ${LIBOQS_VERSION}"
    cd "$BUILD_DIR"
    if [ ! -d liboqs ]; then
        git clone --depth 1 --branch "${LIBOQS_VERSION}" \
            https://github.com/open-quantum-safe/liboqs.git
    fi
    cd liboqs
    mkdir -p build && cd build
    cmake -GNinja -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DBUILD_SHARED_LIBS=ON -DOQS_BUILD_ONLY_LIB=ON ..
    ninja
    sudo ninja install
    sudo ldconfig
    log "liboqs installed"
}

# --- Step 2: Clone plugin and build CoreDNS ---
build_coredns() {
    log "Building CoreDNS ${COREDNS_VERSION} with PQC plugin"
    cd "$BUILD_DIR"

    if [ ! -d dnssec_pqc_plugin ]; then
        git clone --depth 1 --branch "${PLUGIN_BRANCH}" "$PLUGIN_REPO"
    fi

    if [ ! -d coredns ]; then
        git clone --depth 1 --branch "${COREDNS_VERSION}" \
            "${COREDNS_REPO}"
    fi

    cd coredns

    # Register the PQC plugin
    if ! grep -q "dnssec_pqc" plugin.cfg; then
        sed -i '/^dnssec:dnssec$/a dnssec_pqc:github.com/qursa-uc3m/dnssec_pqc_plugin' plugin.cfg
    fi

    # Add PQC plugin dependency and DNS library replacement
    if ! grep -q "qursa-uc3m/dns" go.mod; then
        log "Fetching PQC plugin module"
        GOPROXY=direct go get "github.com/qursa-uc3m/dnssec_pqc_plugin@${PLUGIN_TAG}"

        log "Adding DNS library replacement"
        echo "" >> go.mod
        echo "replace github.com/miekg/dns => ${DNS_REPO} master" >> go.mod

        go mod tidy
    fi

    # Always point to the local plugin copy (picks up any source changes)
    log "Using local plugin from ${BUILD_DIR}/dnssec_pqc_plugin"
    if grep -q "replace github.com/qursa-uc3m/dnssec_pqc_plugin" go.mod; then
        sed -i '/replace github.com\/qursa-uc3m\/dnssec_pqc_plugin/d' go.mod
    fi
    echo "replace github.com/qursa-uc3m/dnssec_pqc_plugin => ${BUILD_DIR}/dnssec_pqc_plugin" >> go.mod

    go generate
    go mod tidy

    log "Compiling CoreDNS"
    CGO_ENABLED=1 go build -o "${BUILD_DIR}/coredns-pqc" -v

    log "CoreDNS binary: ${BUILD_DIR}/coredns-pqc"
}

# --- Step 3: Build keygen ---
build_keygen() {
    log "Building keygen"
    cd "${BUILD_DIR}/dnssec_pqc_plugin/keygen"
    CGO_ENABLED=1 go build -o "${BUILD_DIR}/keygen" -v
    log "Keygen binary: ${BUILD_DIR}/keygen"
}

# --- Step 4: Build Docker image for kind ---
build_image() {
    log "Building Docker image ${IMAGE_NAME}:${IMAGE_TAG}"
    cat > "${BUILD_DIR}/Dockerfile" <<'DOCKERFILE'
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libssl3 && rm -rf /var/lib/apt/lists/*
COPY coredns-pqc /usr/local/bin/coredns
COPY liboqs/build/lib/*.so* /usr/local/lib/
RUN ldconfig
EXPOSE 53 53/udp
ENTRYPOINT ["coredns"]
DOCKERFILE

    cd "$BUILD_DIR"
    docker build -t "${IMAGE_NAME}:${IMAGE_TAG}" -f Dockerfile .
    log "Docker image built: ${IMAGE_NAME}:${IMAGE_TAG}"
}

# --- Main ---
install_liboqs
build_coredns
build_keygen
build_image

log "Build complete."
log "  CoreDNS binary: ${BUILD_DIR}/coredns-pqc"
log "  Keygen binary:  ${BUILD_DIR}/keygen"
log "  Docker image:   ${IMAGE_NAME}:${IMAGE_TAG}"

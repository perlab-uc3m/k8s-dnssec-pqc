#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$repo_dir/build/venv/bin/python"
if [ ! -x "$python_bin" ]; then
    echo "Missing benchmark environment. Run ./bench doctor first." >&2
    exit 1
fi
if ! "$python_bin" -m ruff --version >/dev/null 2>&1; then
    echo "Install formatter: build/venv/bin/python -m pip install -r config/revision-dev.requirements.txt" >&2
    exit 1
fi

cd "$repo_dir"
"$python_bin" -m ruff check src/revision tests
"$python_bin" -m ruff format --check src/revision tests
"$python_bin" -m pytest -q tests
if [ -n "$(gofmt -l tools/keygen_ed/main.go tools/verify/main.go)" ]; then
    echo "Go source is not gofmt-clean." >&2
    exit 1
fi
bash -n bench scripts/bootstrap_revision.sh scripts/build_revision.sh \
    scripts/setup_revision_cluster.sh scripts/deploy_coredns.sh

#!/usr/bin/env bash
# One command for the final revision campaign: ./bench final-revision
# Builds only if the measured build is missing or stale, creates the Kind cluster if
# needed, runs and checks a short smoke campaign, then the main campaign (resumable),
# then exports tables, figures and a checksummed data archive.
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
bash ./scripts/bootstrap_revision.sh
export LD_LIBRARY_PATH="$repo_dir/build/local/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PYTHONPATH="$repo_dir/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
py="$repo_dir/build/venv/bin/python"
results="$repo_dir/results/revision"
main="$results/revision-final"
smoke="$results/revision-final-smoke"
mkdir -p "$results" "$repo_dir/release"
exec 9>"$repo_dir/build/revision-final.lock"
flock -n 9 || { echo "The final revision campaign is already running." >&2; exit 1; }

build_current() {
    "$py" - <<'PY' 2>/dev/null
import json, subprocess, sys
from revision.cli import validated_build_manifest
record = validated_build_manifest()
image = subprocess.check_output(
    ["docker", "image", "inspect", "coredns-pqc:revision", "--format", "{{.Id}}"], text=True
).strip()
sys.exit(0 if image == record["image_id"] and record["signature_ownership_fix"] else 1)
PY
}
if build_current; then
    echo "Reusing the existing build: $(sha256sum build/coredns-pqc | cut -c1-16)"
else
    echo "No valid build found; building (BUILD_JOBS=${BUILD_JOBS:-1})."
    BUILD_JOBS="${BUILD_JOBS:-1}" bash ./scripts/build_revision.sh
fi
bash ./scripts/setup_revision_cluster.sh

# reproduce skips complete runs only after checking the frozen configuration and acquisition hashes.
"$py" -m revision.cli reproduce config/revision-final-smoke.yaml --output "$smoke" --no-build --no-setup
"$py" -m revision.cli check-smoke "$smoke"
date -u +%FT%TZ > "$smoke/SMOKE_PASSED"

# A failed run stops the campaign; completed runs are kept and skipped on the next attempt.
for attempt in 1 2 3; do
    if "$py" -m revision.cli reproduce config/revision-final.yaml --output "$main" --no-build --no-setup; then
        break
    fi
    if [ "$attempt" = 3 ]; then
        echo "The main campaign failed three times; see FAILED files under $main/runs." >&2
        exit 1
    fi
    echo "Campaign stopped on a failed run; resuming in 30 s (attempt $((attempt + 1)) of 3)." >&2
    sleep 30
done

export_dir="$main/export"
reference_args=()
if [ -f "$results/acquisition_manifest.json" ] && [ -d "$results/runs" ]; then
    reference_args=(--reference "$results")  # the 30 September campaign, for comparison only
fi
# Tables and figures of the article: the twelve reference configurations, then all blocks.
"$py" -m revision.cli export-paper "$main" --output "$export_dir/paper"
"$py" -m revision.cli export-final "$main" --output "$export_dir/final" "${reference_args[@]}"
"$py" -m revision.cli package "$smoke" "$main" --output "$repo_dir/release/revision-final-data.tar"
"$py" -m revision.cli verify-package "$repo_dir/release/revision-final-data.tar" "$repo_dir/release/revision-final-data.tar.sha256.json"
echo
echo "Done. Tables, figures and quoted numbers: $export_dir/paper and $export_dir/final"
echo "Data archive and SHA256 manifest: $repo_dir/release/revision-final-data.tar*"

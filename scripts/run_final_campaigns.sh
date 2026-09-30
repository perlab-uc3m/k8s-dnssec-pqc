#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"
./scripts/bootstrap_revision.sh
mkdir -p build results/revision/final-suite release
exec 9>build/final-suite.lock
flock -n 9 || { echo "A final suite is already running." >&2; exit 1; }

suite_id="${BENCH_SUITE_ID:-final-$(date -u +%Y%m%dT%H%M%S)}"
if [[ ! "$suite_id" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
    echo "BENCH_SUITE_ID may contain only letters, numbers, dot, underscore and hyphen." >&2
    exit 2
fi
suite_root="$repo_dir/results/revision/final-suite"
suite_dir="$suite_root/$suite_id"
output_root="$repo_dir/results/revision/$suite_id"
config_dir="$suite_dir/config"
stages=(freshness-observer-final freshness-main-final freshness-pressure-final \
    freshness-sensitivity-final freshness-replicas-final)
outputs=()
for stage in "${stages[@]}"; do outputs+=("$output_root/$stage"); done
mkdir -p "$suite_dir" "$config_dir" "$output_root" "$repo_dir/release"
printf '%s\n' "$suite_id" > "$suite_root/LATEST"
export BUILD_JOBS="${BUILD_JOBS:-1}"
export PYTHONUNBUFFERED=1

if [ "${BENCH_NO_BUILD:-0}" != 1 ]; then ./scripts/build_revision.sh; fi
# Freeze sources once, and refuse to mix changed acquisition code into a resumed suite.
build/venv/bin/python - "$suite_id" "$config_dir" <<'PY'
import hashlib
import json
import shutil
import sys
from pathlib import Path
import yaml
root = Path.cwd()
from src.revision.cli import config, validated_build_manifest
build = validated_build_manifest()
build_sources = {"signature_ownership_fix": build["signature_ownership_fix"], "inputs": build["inputs"]}
sources = [root / "bench", root / "pyproject.toml", root / "README.md"]
for directory in ("src/revision", "scripts", "patches", "tools", "config", "tests", "docs", "k8s"):
    sources.extend(p for p in (root / directory).rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc")
hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
guarded = ["src/revision/" + name + ".py" for name in ("cli", "metrics", "queries", "workload", "resources")]
guarded += ["scripts/deploy_coredns.sh", "scripts/run_final_campaigns.sh"]
stages = ("freshness-observer-final", "freshness-main-final", "freshness-pressure-final",
          "freshness-sensitivity-final", "freshness-replicas-final")
guarded += [f"config/{stage}.yaml" for stage in stages]
record = {"build_source_manifest": build_sources,
          "acquisition_and_configs": {p: hashes[p] for p in guarded}}
guard = root / "results/revision/final-suite" / sys.argv[1] / "source_guard.json"
if guard.exists():
    if json.loads(guard.read_text()) != record:
        raise RuntimeError("Final suite source or build inputs changed; use a new BENCH_SUITE_ID.")
else:
    snapshot = root / "results/revision/final-suite" / sys.argv[1] / "source_snapshot"
    for source in sources:
        target = snapshot / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (snapshot / "snapshot-manifest.json").write_text(json.dumps(hashes, indent=2) + "\n")
guard.write_text(json.dumps(record, indent=2) + "\n")
config_dir = Path(sys.argv[2])
for stage in stages:
    doc = yaml.safe_load((root / "config" / f"{stage}.yaml").read_text())
    doc["campaign_id"] = f"{stage}-{sys.argv[1]}"
    effective = config_dir / f"{stage}.yaml"
    rendered = yaml.safe_dump(doc, sort_keys=False)
    if effective.exists() and effective.read_text() != rendered:
        raise RuntimeError(f"Frozen configuration changed: {effective}")
    effective.write_text(rendered)
    config(effective)
    print(f"{stage}: {len(doc['cells'])} cells x {doc['repetitions']} repetitions")
PY

stage=setup
started=$(date -u +%FT%TZ)
if [ -e "$suite_dir/COMPLETE" ]; then echo "Suite $suite_id is already complete." >&2; exit 2; fi
printf '%s\n' "$$ $started" > "$suite_dir/RUNNING"
rm -f "$suite_dir/FAILED"
finish() {
    code=$?
    trap - EXIT
    if [ "$code" -ne 0 ]; then
        printf '%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$stage" "$code" > "$suite_dir/FAILED"
    fi
    ./bench cleanup || true
    rm -f "$suite_dir/RUNNING"
    exit "$code"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
./scripts/setup_revision_cluster.sh
for stage in "${stages[@]}"; do
    printf '%s\t%s\tstarted\n' "$(date -u +%FT%TZ)" "$stage" >> "$suite_dir/status.tsv"
    ./bench reproduce "$config_dir/$stage.yaml" --output "$output_root/$stage" --no-build --no-setup
    printf '%s\t%s\tcomplete\n' "$(date -u +%FT%TZ)" "$stage" >> "$suite_dir/status.tsv"
done
stage=package
mkdir -p "$output_root/freshness-main-final/source_snapshot"
cp -a "$suite_dir/source_snapshot/." "$output_root/freshness-main-final/source_snapshot/"
cp "$suite_dir/source_guard.json" "$suite_dir/status.tsv" "$output_root/freshness-main-final/"
./bench package "${outputs[@]}" --output "release/freshness-$suite_id.tar"
./bench verify-package "release/freshness-$suite_id.tar" "release/freshness-$suite_id.tar.sha256.json"
printf '%s\n' "$(date -u +%FT%TZ)" > "$suite_dir/COMPLETE"
echo "Final suite $suite_id completed. Reports: $output_root/"

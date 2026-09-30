# Freshness benchmark

This benchmark measures how DNSSEC signing, response caching, endpoint changes and CPU limits affect DNS answers for Kubernetes headless Services. A query counts as timely and fresh only when it receives a positive answer before the reporting deadline, the answer passes offline signature verification, and the address matches the latest acknowledged EndpointSlice update at answer completion. Every offered query stays in the denominator. The verifier checks positive answers against the saved private-zone public key; it does not validate a public delegation chain or negative proofs.

## Run the complete campaign

On Linux x86-64, install Docker, Go, CMake, Ninja, GCC, Git and Python with venv support. The first run downloads pinned Kind and kubectl binaries, builds the pinned CoreDNS and liboqs sources, and loads the image into a local Kind cluster. Use one build job to reduce peak memory. The cluster container is capped at 4 GiB and each CoreDNS pod at 512 MiB. Aim for 6 to 8 GiB of available host memory before starting. `./bench doctor` checks local tools, ports and memory.

```bash
./bench doctor
BUILD_JOBS=1 systemd-inhibit --what=sleep:idle --mode=block \
  --why="DNSSEC measurement campaign" ./bench final
```

The complete campaign has 204 runs and at least 8 hours 16 minutes of warmup and measurement, plus build, deployment, verification and analysis. It runs sequentially on one host. The five fixed configurations are:

| Configuration | Cells x repetitions | Comparison |
|---|---:|---|
| `freshness-observer-final.yaml` | 3 x 3 | Unsigned client load and collection overhead |
| `freshness-main-final.yaml` | 36 x 3 | Algorithm, response-cache TTL, endpoint update rate and CPU quota |
| `freshness-pressure-final.yaml` | 4 x 3 | Signature-cache capacity and observed evictions |
| `freshness-sensitivity-final.yaml` | 16 x 3 | Bursts, service popularity, transport and a constrained cache contrast |
| `freshness-replicas-final.yaml` | 9 x 3 | One or two pods at matched and increased total CPU quotas |

The main comparison uses 32 fixed headless names, Ed25519, ML-DSA-44 and Falcon-512, 100 offered queries/s, static or 16 updates/s, response-cache ceilings of 0, 1 or 5 seconds, and pod CPU quotas of 0.125 or 0.5 core. The server signs real messages. Update addresses are synthetic and have no application behind them. Falcon's algorithm identifier is used only in this private test zone; it conflicts with the public IANA allocation.

## Results and resumption

Each invocation gets a timestamped suite ID. `results/revision/final-suite/LATEST` contains it. The suite status, effective YAML configurations, source snapshot and `RUNNING`, `FAILED` or `COMPLETE` marker are in `results/revision/final-suite/<suite-id>/`. The corresponding campaign directories are `results/revision/<suite-id>/<configuration-name>/`.

Each campaign contains `report.md`, per-run and per-cell CSV tables, figures and `runs/<cell>/rep-XX/attempt-XX/`. A run retains query timings, sent attempts, received DNS frames, EndpointSlice update times, offline verification results, per-pod CPU/cgroup metrics, host resource samples and a derived summary. Missing CPU samples do not remove completed query outcomes. A failed or interrupted attempt stays in place; the runner resumes only complete repetitions. Randomized cell order and workload seeds are saved.

The completed suite produces `release/freshness-<suite-id>.tar` and `release/freshness-<suite-id>.tar.sha256.json`, then verifies the archive and removes its dedicated Kind cluster. The archive contains all five campaigns and the source snapshot. Build objects, raw results and archives are excluded from Git.

To resume an interrupted run on the same machine with the same validated build:

```bash
bench_suite_id=$(cat results/revision/final-suite/LATEST)
BENCH_SUITE_ID="$bench_suite_id" BENCH_NO_BUILD=1 BUILD_JOBS=1 \
  systemd-inhibit --what=sleep:idle --mode=block \
  --why="DNSSEC measurement campaign" ./bench final
```

The campaign fingerprint rejects changed acquisition code or configurations during resumption. A new machine should start a fresh suite ID. Individual campaigns can be replotted offline with `./bench report results/revision/<suite-id>/<configuration-name>`.

These measurements characterize a controlled, single-host Kubernetes path. They do not measure application routing failures, WAN conditions, energy, or cross-host scaling. The plotted 10 and 50 ms deadlines are reporting thresholds, not prescribed Kubernetes targets. Interpret three repetitions as paired run values and ranges rather than independent query-level confidence intervals.

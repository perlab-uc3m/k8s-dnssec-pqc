# PQC DNSSEC Kubernetes Benchmark

Benchmarking post-quantum DNSSEC signing performance under Kubernetes service churn.

This tool measures how cryptographic load scales across 9 post-quantum and 3 classical signature algorithms in a Kind cluster running a PQC-enabled CoreDNS.

## Supported algorithms

| ID | Name | Type | Transport |
|----|------|------|-----------|
| 0  | NONE (Baseline) | Unsigned | UDP |
| 8  | RSA-SHA256 | Classical | UDP |
| 13 | ECDSA-P256 | Classical | UDP |
| 15 | Ed25519 | Classical | UDP |
| 17 | FALCON-512 | PQC | UDP |
| 18 | ML-DSA-44 | PQC | TCP |
| 19 | SLH-DSA-SHA2-128s | PQC | TCP |
| 20 | MAYO-1 | PQC | UDP |
| 21 | SNOVA | PQC | UDP |
| 27 | FALCON-1024 | PQC | TCP |
| 28 | ML-DSA-65 | PQC | TCP |
| 30 | MAYO-3 | PQC | UDP |
| 38 | ML-DSA-87 | PQC | TCP |

Algorithms with signed responses above 1232 bytes (the EDNS(0) UDP limit) fall back to TCP, paying an additional round-trip penalty that grows with network latency.

## Prerequisites

- Go 1.23+
- Python 3.10+
- liboqs 0.14.0-rc1 (built automatically by `scripts/build.sh`)
- Docker, Kind, kubectl

Python dependencies:

```bash
pip install -r requirements.txt
```

## Repository structure

```
├── run.py                  # CLI entry point
├── config/
│   └── benchmark.yaml      # Campaign parameters
├── scripts/
│   ├── build.sh            # Build liboqs, CoreDNS, keygen, Docker image
│   ├── setup_cluster.sh    # Create Kind cluster
│   ├── deploy_coredns.sh   # Deploy CoreDNS with a given algorithm
│   ├── netem.sh            # Apply tc-netem network emulation
│   └── parse_bench.py      # Parse Go signing microbenchmark output
├── src/
│   ├── runner.py           # Benchmark orchestrator
│   ├── workload.py         # Service churn generator
│   ├── query.py            # Concurrent DNS query sender
│   ├── collector.py        # Prometheus + container metrics scraping
│   ├── analyzer.py         # Result loading, Sigma computation, summaries
│   └── plotter.py          # Paper-quality figure generation
├── k8s/
│   └── kind-config.yaml    # Kind cluster configuration
└── results/                # Campaign outputs (gitignored)
```

## Build

```bash
scripts/build.sh
```

This builds liboqs, CoreDNS with the [PQC DNSSEC plugin](https://github.com/qursa-uc3m/dnssec_pqc_plugin), the key generation tool, and the Docker image.

## Cluster setup

```bash
scripts/setup_cluster.sh
scripts/deploy_coredns.sh <algo_name> <algo_id> [cache_ttl] [type] [sig_cache_cap] [sim_delay] [sim_stddev]
```

The deploy script handles three algorithm types:
- **pqc / classical**: generates DNSSEC keys and configures the `dnssec_pqc` plugin
- **baseline**: deploys CoreDNS without DNSSEC signing (measures the unsigned latency $L_0$)

## Running benchmarks

```bash
python run.py --smoke             # 2 algorithms, 1 parameter point
python run.py --pilot             # 2 algorithms, 4 points
python run.py --short             # all algorithms, reduced grid
python run.py --validation        # all algorithms + simulated delays, minimal grid
python run.py --high-sigma        # TTL sweep, saturation push (288 runs)
python run.py --sigma-sweep       # dense phase-transition grid (672 runs)
python run.py --simulated-sweep   # synthetic signing delay sweep (972 runs)
python run.py --netem-sweep       # network emulation (latency + bandwidth)
python run.py                     # full campaign
```

All campaigns write results to `results/<timestamp>/`. Add `--resume` to skip completed runs.

## Simulated signing delay

The `--simulated-sweep` campaign injects a configurable delay inside the plugin's singleflight closure to synthesize arbitrary signing costs without changing algorithms. This isolates the effect of $\bar{s}$ from other algorithm-specific properties like key size and transport mode.

The delay is controlled via `simulated_delay` / `simulated_stddev` Corefile directives.

## Network emulation

The `--netem-sweep` campaign uses `tc-netem` on the Kind worker node to add one-way latency and bandwidth limits. This reveals how signature compactness determines performance under realistic network conditions: UDP-only algorithms (compact signatures) scale as ~1 RTT, while TCP-fallback algorithms pay ~3 RTT.

## Observability

The `dnssec_pqc` plugin exposes Prometheus metrics on port 30153:

| Metric | Type | Description |
|--------|------|-------------|
| `coredns_dnssec_pqc_cache_size` | Gauge | Signature cache entries |
| `coredns_dnssec_pqc_cache_hits_total` | Counter | Cache hits |
| `coredns_dnssec_pqc_cache_misses_total` | Counter | Cache misses |
| `coredns_dnssec_pqc_sign_duration_seconds` | Histogram | Per-signing wall time |
| `coredns_dnssec_pqc_singleflight_execs_total` | Counter | Signing executions |
| `coredns_dnssec_pqc_singleflight_coalesced_total` | Counter | Coalesced callers |

These metrics are scraped automatically after each run and stored in `meta.json`.

## Analysis and plots

```bash
python run.py --analyze                     # print summaries
python run.py --plot                        # generate figures (PDF + PNG)
python run.py --plot --campaign-id <dir>    # target a specific campaign
```

Auto-selects the latest campaign directory when no `--campaign-id` is given.

## Signing microbenchmark

Standalone Go benchmark measuring raw signing time per algorithm. Its output feeds the analytical model ($\bar{s}$, $C_v$).

```bash
cd build/dnssec_pqc_plugin/bench
go test -bench=BenchmarkSign -benchtime=100x -count=1 -timeout=30m \
    | tee bench_results_100.txt
python3 scripts/parse_bench.py < bench_results_100.txt
```

## Configuration

All parameters live in [`config/benchmark.yaml`](config/benchmark.yaml).

## License

MIT. See [LICENSE](LICENSE) for details.

"""Result analysis: cryptographic load (Sigma), coefficient of variation,
and regime classification from raw benchmark data."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

# Canonical path for microbenchmark results written by plot_signing_times.py.
_SIGNING_BENCH_JSON = Path(__file__).resolve().parent.parent / "results" / "signing_bench.json"


@dataclass
class RunSummary:
    algorithm: str
    churn_rate: float
    query_rate: float
    n_queries: int
    latency_p50: float
    latency_p95: float
    latency_p99: float
    latency_mean: float
    latency_std: float
    failure_rate: float
    sigma: float | None = None
    cs: float | None = None
    sig_cache_cap: int | None = None
    cache_ttl: float | None = None
    simulated_delay_ms: float | None = None
    # Prometheus-scraped signing metrics
    cache_hits: float | None = None
    cache_misses: float | None = None
    cache_hit_rate: float | None = None
    sign_duration_mean_ms: float | None = None
    singleflight_coalesced: float | None = None
    singleflight_execs: float | None = None
    # Response size stats (from queries.jsonl)
    response_size_mean: float | None = None
    response_size_min: float | None = None
    response_size_max: float | None = None
    # Transport breakdown
    tcp_fraction: float | None = None
    unsigned_fraction: float | None = None
    transport_observed: bool = False
    # Throughput
    actual_qps: float | None = None
    # Container resources (from kubectl top)
    cpu_millicores_mean: float | None = None
    cpu_millicores_max: float | None = None
    memory_mib_mean: float | None = None
    memory_mib_max: float | None = None
    # Server-side CoreDNS metrics
    server_latency_mean_ms: float | None = None
    # Network emulation (tc-netem)
    network_delay_ms: int | None = None
    network_rate_kbps: int | None = None


def load_run(run_dir: Path) -> tuple[dict, list[dict]]:
    meta = json.loads((run_dir / "meta.json").read_text())
    queries = []
    with open(run_dir / "queries.jsonl") as f:
        for line in f:
            queries.append(json.loads(line))
    return meta, queries


def compute_summary(meta: dict, queries: list[dict]) -> RunSummary:
    latencies = np.array([q["latency_ms"] for q in queries if q["error"] is None])
    errors = sum(1 for q in queries if q["error"] is not None)
    ok_queries = [q for q in queries if q["error"] is None]

    if len(latencies) == 0:
        return RunSummary(
            algorithm=meta["algorithm"],
            churn_rate=meta["churn_rate"],
            query_rate=meta["query_rate"],
            n_queries=len(queries),
            latency_p50=float("nan"),
            latency_p95=float("nan"),
            latency_p99=float("nan"),
            latency_mean=float("nan"),
            latency_std=float("nan"),
            failure_rate=1.0,
        )

    # Response size stats
    resp_sizes = np.array([q["response_size"] for q in ok_queries])
    response_size_mean = float(np.mean(resp_sizes)) if len(resp_sizes) > 0 else None
    response_size_min = float(np.min(resp_sizes)) if len(resp_sizes) > 0 else None
    response_size_max = float(np.max(resp_sizes)) if len(resp_sizes) > 0 else None

    # Transport breakdown: fraction that used TCP
    # New format has "transport" field; legacy data inferred from response_size
    tcp_count = 0
    transport_observed = False
    if ok_queries and "transport" in ok_queries[0]:
        transport_observed = True
        tcp_count = sum(1 for q in ok_queries if q["transport"] == "tcp")
    else:
        tcp_count = sum(1 for q in ok_queries if q["response_size"] > 1232)
    tcp_fraction = tcp_count / len(ok_queries) if ok_queries else None

    # Unsigned fraction: queries without RRSIG
    if ok_queries and "had_rrsig" in ok_queries[0]:
        unsigned_count = sum(1 for q in ok_queries if not q["had_rrsig"])
    else:
        unsigned_count = 0
    unsigned_fraction = unsigned_count / len(ok_queries) if ok_queries else None

    # Actual throughput (QPS)
    timestamps = sorted(q["timestamp"] for q in ok_queries)
    if len(timestamps) >= 2:
        span = timestamps[-1] - timestamps[0]
        actual_qps = (len(timestamps) - 1) / span if span > 0 else None
    else:
        actual_qps = None

    return RunSummary(
        algorithm=meta["algorithm"],
        churn_rate=meta["churn_rate"],
        query_rate=meta["query_rate"],
        n_queries=len(queries),
        latency_p50=float(np.percentile(latencies, 50)),
        latency_p95=float(np.percentile(latencies, 95)),
        latency_p99=float(np.percentile(latencies, 99)),
        latency_mean=float(np.mean(latencies)),
        latency_std=float(np.std(latencies)),
        failure_rate=errors / len(queries) if queries else 0.0,
        response_size_mean=response_size_mean,
        response_size_min=response_size_min,
        response_size_max=response_size_max,
        tcp_fraction=tcp_fraction,
        unsigned_fraction=unsigned_fraction,
        transport_observed=transport_observed,
        actual_qps=actual_qps,
    )


# Fallback signing times (ms). Overridden by results/signing_bench.json when available.
_SIGNING_TIMES_FALLBACK = {
    "Falcon-512": 0.2136,
    "Falcon-1024": 0.4333,
    "ECDSAP256SHA256": 0.0365,
    "RSASHA256": 0.0,
    "SNOVA_24_5_4": 0.4386,
    "MAYO-1": 0.1531,
    "MAYO-3": 0.0,
    "ML-DSA-44": 0.0811,
    "ML-DSA-65": 0.1299,
    "ML-DSA-87": 0.0,
    "ED25519": 0.0234,
    "SPHINCS+-SHA2-128s-simple": 156.584,
}

# TCP transport overhead (ms): algorithms with responses > 1232 B trigger TCP fallback.
TRANSPORT_OVERHEAD_MS: dict[str, float] = {
    "Falcon-512": 0.0,  # ~779 B  → UDP
    "Falcon-1024": 1.0,  # ~1395 B → TCP
    "ML-DSA-44": 1.0,  # ~2546 B → TCP
    "ML-DSA-65": 1.0,  # ~3435 B → TCP
    "ML-DSA-87": 1.0,  # ~4886 B → TCP
    "MAYO-1": 0.0,  # ~580 B  → UDP
    "MAYO-3": 0.0,  # ~836 B  → UDP
    "SNOVA_24_5_4": 0.0,  # ~374 B  → UDP
    "ECDSAP256SHA256": 0.0,  # ~190 B  → UDP
    "ED25519": 0.0,  # ~190 B  → UDP
    "RSASHA256": 0.0,  # ~515 B  → UDP
    "SPHINCS+-SHA2-128s-simple": 1.0,  # ~7982 B → TCP
}

_SIGNING_CV_FALLBACK = {
    "Falcon-512": 0.0467,
    "ML-DSA-44": 0.0856,
    "MAYO-1": 0.0329,
    "SNOVA_24_5_4": 0.0385,
    "Falcon-1024": 0.0851,
    "ML-DSA-65": 0.0462,
    "ECDSAP256SHA256": 0.6696,
    "ED25519": 0.0471,
    "SPHINCS+-SHA2-128s-simple": 0.0336,
    "RSASHA256": 0.0,
    "MAYO-3": 0.0,
    "ML-DSA-87": 0.0,
}


def _load_signing_bench() -> tuple[dict[str, float], dict[str, float]]:
    """Load signing times and CVs from microbenchmark JSON."""
    times = dict(_SIGNING_TIMES_FALLBACK)
    cvs = dict(_SIGNING_CV_FALLBACK)
    if _SIGNING_BENCH_JSON.exists():
        try:
            bench = json.loads(_SIGNING_BENCH_JSON.read_text())
            for algo, stats in bench.items():
                times[algo] = stats["mean_ms"]
                cvs[algo] = stats["cv"]
            log.info(
                "Loaded signing bench from %s (%d algorithms)", _SIGNING_BENCH_JSON, len(bench)
            )
        except Exception as e:
            log.warning("Failed to load %s: %s (using fallback)", _SIGNING_BENCH_JSON, e)
    return times, cvs


SIGNING_TIMES_MS, SIGNING_CV = _load_signing_bench()


def compute_sigma(
    churn_rate: float,
    query_rate: float,
    cache_ttl: float,
    mean_signing_ms: float,
    n_services: int = 20,
    sig_cache_cap: int | None = None,
) -> float:
    """Cryptographic load Sigma = lambda_eff * s_bar."""
    sig_cache_on = sig_cache_cap is None or sig_cache_cap > 0
    resp_cache_on = cache_ttl > 0 and n_services > 0

    if not sig_cache_on and not resp_cache_on:
        # No caching: every query triggers signing
        lambda_eff = query_rate
    elif not sig_cache_on and resp_cache_on:
        # Response cache only
        inv_rate_per_record = churn_rate / n_services
        effective_ttl = min(
            cache_ttl,
            1.0 / inv_rate_per_record if inv_rate_per_record > 0 else float("inf"),
        )
        p_miss = (
            min(1.0, 1.0 / (query_rate * effective_ttl)) if query_rate * effective_ttl > 0 else 1.0
        )
        lambda_eff = query_rate * p_miss
    elif sig_cache_on and not resp_cache_on:
        # Sig cache only: only churn-invalidated records need re-signing
        lambda_eff = churn_rate
    else:
        # Both caches: sig cache absorbs most response-cache misses;
        # only brand-new records from churn need signing
        lambda_eff = churn_rate

    s_bar = mean_signing_ms / 1000.0
    return lambda_eff * s_bar


def analyze_all(results_dir: Path, cache_ttl: float = 5.0) -> list[RunSummary]:
    summaries = []
    for algo_dir in sorted(results_dir.iterdir()):
        if not algo_dir.is_dir():
            continue
        for param_dir in sorted(algo_dir.iterdir()):
            if not param_dir.is_dir():
                continue
            for run_dir in sorted(param_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                try:
                    meta, queries = load_run(run_dir)
                    summary = compute_summary(meta, queries)
                    algo_name = meta["algorithm"]
                    # Use per-run TTL from meta.json; fall back to global default
                    run_ttl = meta.get("cache_ttl", cache_ttl)
                    sim_del = meta.get("simulated_delay_ms")
                    # Effective signing time: simulated delay overrides real when set
                    if sim_del is not None:
                        eff_signing_ms = sim_del + SIGNING_TIMES_MS.get(algo_name, 0)
                    elif algo_name in SIGNING_TIMES_MS:
                        eff_signing_ms = SIGNING_TIMES_MS[algo_name]
                    else:
                        eff_signing_ms = None
                    if eff_signing_ms is not None:
                        scc = meta.get("sig_cache_cap")
                        summary.sigma = compute_sigma(
                            meta["churn_rate"],
                            meta["query_rate"],
                            run_ttl,
                            eff_signing_ms,
                            sig_cache_cap=scc,
                        )
                        summary.cs = SIGNING_CV.get(algo_name)
                    summary.sig_cache_cap = meta.get("sig_cache_cap")
                    summary.cache_ttl = meta.get("cache_ttl", 0.0)
                    summary.simulated_delay_ms = sim_del
                    summary.network_delay_ms = meta.get("network_delay_ms")
                    summary.network_rate_kbps = meta.get("network_rate_kbps")
                    # Prometheus-scraped signing metrics
                    sm = meta.get("signing_metrics", {})
                    if sm:
                        summary.cache_hits = sm.get("cache_hits")
                        summary.cache_misses = sm.get("cache_misses")
                        summary.cache_hit_rate = sm.get("cache_hit_rate")
                        summary.sign_duration_mean_ms = sm.get("sign_duration_mean_ms")
                        summary.singleflight_coalesced = sm.get("singleflight_coalesced")
                        summary.singleflight_execs = sm.get("singleflight_execs")
                    # Container resource metrics (kubectl top)
                    cr = meta.get("container_resources", {})
                    if cr:
                        summary.cpu_millicores_mean = cr.get("cpu_millicores_mean")
                        summary.cpu_millicores_max = cr.get("cpu_millicores_max")
                        summary.memory_mib_mean = cr.get("memory_mib_mean")
                        summary.memory_mib_max = cr.get("memory_mib_max")
                    # Standard CoreDNS Prometheus metrics
                    cm = meta.get("coredns_metrics", {})
                    if cm:
                        dur = cm.get("request_duration_seconds", {})
                        if dur.get("count", 0) > 0:
                            summary.server_latency_mean_ms = (dur["sum"] / dur["count"]) * 1000.0
                    summaries.append(summary)
                except Exception as e:
                    log.warning("Failed to process %s: %s", run_dir, e)
    return summaries

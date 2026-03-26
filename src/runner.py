"""Benchmark orchestrator: coordinates workload, queries, and metrics
collection across the full parameter grid."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.workload import run_churn, cleanup, LiveServicePool
from src.query import run_queries, QueryResult
from src.collector import (
    collect_pod_resources,
    scrape_signing_metrics,
    scrape_coredns_metrics,
    collect_container_resources,
)

log = logging.getLogger(__name__)

REPO_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_DIR / "scripts"
DNS_HOST = "127.0.0.1"
DNS_PORT = 30053
DNS_TCP_PORT = 30054

MIN_FREE_RAM_BYTES = int(float(os.environ.get("BENCH_MIN_FREE_RAM_GB", "0.5")) * (1024**3))
RUN_COOLDOWN = int(os.environ.get("BENCH_COOLDOWN", "5"))
COMPACT_EVERY_N_RUNS = int(os.environ.get("BENCH_COMPACT_EVERY", "3"))
KIND_CLUSTER_NAME = os.environ.get("KIND_CLUSTER_NAME", "pqc-dnssec-bench")


def _apply_netem(delay_ms: int | None, jitter_ms: int = 0, rate_kbps: int | None = None):
    """Apply or remove tc-netem delay + bandwidth limit on the Kind worker.

    Uses scripts/netem.sh which runs ``tc qdisc`` inside the worker
    Docker container.  A one-way *delay_ms* makes 1 RTT ≈ 2*delay_ms,
    amplifying the TCP handshake cost for algorithms whose signed
    responses exceed the EDNS(0) 1232-byte threshold.

    An optional *rate_kbps* limits egress bandwidth so that response
    size becomes a continuous (not binary) performance variable:
    transfer_time ≈ response_size / rate.
    """
    script = str(SCRIPTS_DIR / "netem.sh")
    no_delay = delay_ms is None or delay_ms <= 0
    no_rate = rate_kbps is None or rate_kbps <= 0
    if no_delay and no_rate:
        subprocess.run([script, "remove"], check=False, capture_output=True)
        log.info("netem: removed (no delay, no rate limit)")
    else:
        d = delay_ms if delay_ms and delay_ms > 0 else 0
        cmd = [
            script,
            "apply",
            str(d),
            str(jitter_ms),
            str(rate_kbps if rate_kbps and rate_kbps > 0 else 0),
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        parts = []
        if d > 0:
            parts.append(f"{d} ms delay")
        if rate_kbps and rate_kbps > 0:
            parts.append(f"{rate_kbps} kbps")
        log.info("netem: applied %s", " + ".join(parts))


def check_system_resources():
    """Abort if system memory is critically low."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    avail_kb = int(line.split()[1])
                    avail_bytes = avail_kb * 1024
                    avail_gb = avail_bytes / (1024**3)
                    if avail_bytes < MIN_FREE_RAM_BYTES:
                        raise MemoryError(
                            f"Available RAM too low: {avail_gb:.1f} GB "
                            f"(minimum: {MIN_FREE_RAM_BYTES / (1024**3):.1f} GB). "
                            f"Aborting to prevent system crash."
                        )
                    log.debug("Available RAM: %.1f GB", avail_gb)
                    return
    except FileNotFoundError:
        pass  # Non-Linux system, skip check


def ensure_cluster_healthy(max_retries: int = 3, wait: float = 30.0):
    """Check that the K8s API server is reachable; restart the Kind control-plane if not."""
    for attempt in range(max_retries):
        result = subprocess.run(
            ["kubectl", "get", "nodes", "--request-timeout=5s"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return
        log.warning(
            "API server unreachable (attempt %d/%d), recovering...", attempt + 1, max_retries
        )
        # Restart the Kind control-plane Docker container
        cp_container = f"{KIND_CLUSTER_NAME}-control-plane"
        subprocess.run(["docker", "restart", cp_container], capture_output=True)
        log.info("Restarted %s, waiting %.0fs for API server...", cp_container, wait)
        time.sleep(wait)
    # Final check
    result = subprocess.run(
        ["kubectl", "get", "nodes", "--request-timeout=10s"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Cluster unrecoverable after restart attempts. Aborting.")


def load_config(path: Path | None = None) -> dict:
    if path is None:
        path = REPO_DIR / "config" / "benchmark.yaml"
    with open(path) as f:
        return yaml.safe_load(f)


def get_coredns_ip() -> str:
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            "kube-system",
            "get",
            "svc",
            "coredns-pqc",
            "-o",
            "jsonpath={.spec.clusterIP}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def get_active_service_names(namespace: str) -> list[str]:
    result = subprocess.run(
        [
            "kubectl",
            "-n",
            namespace,
            "get",
            "svc",
            "-l",
            "bench=churn",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    names = result.stdout.strip().split()
    return names if names != [""] else []


def deploy_algorithm(
    algo_name: str,
    algo_id: int,
    cache_ttl: int,
    algo_type: str = "pqc",
    sig_cache_cap: int | None = None,
    sim_delay_ms: float | None = None,
    sim_stddev_ms: float | None = None,
) -> str:
    log.info(
        "Deploying CoreDNS with %s (id=%d, type=%s, sig_cache=%s, sim_delay=%s)",
        algo_name,
        algo_id,
        algo_type,
        sig_cache_cap,
        sim_delay_ms,
    )
    cmd = [
        str(SCRIPTS_DIR / "deploy_coredns.sh"),
        algo_name,
        str(algo_id),
        str(cache_ttl),
        algo_type,
    ]
    if sig_cache_cap is not None:
        cmd.append(str(sig_cache_cap))
    elif sim_delay_ms is not None:
        cmd.append("")  # placeholder for sig_cache_cap positional arg
    if sim_delay_ms is not None:
        cmd.append(f"{sim_delay_ms}ms")
        cmd.append(f"{sim_stddev_ms or 0}ms")
    subprocess.run(cmd, check=True)
    return get_coredns_ip()


def _wait_dns_ready(max_attempts: int = 15, interval: float = 2.0):
    """Send probe queries to CoreDNS until it actually responds.

    Replaces a fixed sleep after deploy.  Kubernetes marks pods as Ready
    based on container state, not on whether CoreDNS has loaded its plugins.
    This ensures the server is truly serving before we start measurements.
    """
    import dns.resolver

    for attempt in range(max_attempts):
        try:
            resolver = dns.resolver.Resolver(configure=False)
            resolver.nameservers = [DNS_HOST]
            resolver.port = DNS_PORT
            resolver.lifetime = 3
            resolver.resolve("kubernetes.default.svc.cluster.local", "A")
            log.info("CoreDNS ready after %d probe(s)", attempt + 1)
            return
        except Exception:
            if attempt < max_attempts - 1:
                time.sleep(interval)
    raise RuntimeError("CoreDNS not responding after deploy (timed out on readiness probes)")


def save_results(
    output_dir: Path,
    query_results: list[QueryResult],
    pod_metrics: dict | None = None,
    run_meta: dict | None = None,
):
    output_dir.mkdir(parents=True, exist_ok=True)

    # Query results as JSONL
    with open(output_dir / "queries.jsonl", "w") as f:
        for r in query_results:
            f.write(
                json.dumps(
                    {
                        "timestamp": r.timestamp,
                        "service": r.service_name,
                        "latency_ms": r.latency_ms,
                        "rcode": r.rcode,
                        "response_size": r.response_size,
                        "had_rrsig": r.had_rrsig,
                        "transport": r.transport,
                        "error": r.error,
                    }
                )
                + "\n"
            )

    if run_meta:
        with open(output_dir / "meta.json", "w") as f:
            json.dump(run_meta, f, indent=2)

    if pod_metrics:
        with open(output_dir / "pod_metrics.json", "w") as f:
            json.dump(pod_metrics, f, indent=2)


def run_single(
    algo_name: str,
    algo_id: int,
    churn_rate: float,
    query_rate: float,
    duration: float,
    warmup: float,
    baseline_services: int,
    cache_ttl: int,
    namespace: str,
    domain: str,
    output_dir: Path,
    algo_type: str = "pqc",
    sig_cache_cap: int | None = None,
    skip_deploy: bool = False,
    sim_delay_ms: float | None = None,
    sim_stddev_ms: float | None = None,
    network_delay_ms: int | None = None,
    network_rate_kbps: int | None = None,
):
    """Run a single benchmark point."""
    log.info(
        "--- Run: algo=%s churn=%.1f qps=%.0f sim_delay=%s nd=%s nr=%s ---",
        algo_name,
        churn_rate,
        query_rate,
        sim_delay_ms,
        network_delay_ms,
        network_rate_kbps,
    )

    # Deploy CoreDNS with the algorithm (caller may skip if config unchanged)
    if not skip_deploy:
        deploy_algorithm(
            algo_name, algo_id, cache_ttl, algo_type, sig_cache_cap, sim_delay_ms, sim_stddev_ms
        )
        _wait_dns_ready()  # verify CoreDNS actually responds before proceeding

    # Start churn in background with a shared live-name pool so queries
    # always target currently-active services.
    pool = LiveServicePool()
    churn_thread_result = {}

    def churn_worker():
        churn_thread_result["state"] = run_churn(
            namespace=namespace,
            churn_rate=churn_rate,
            duration=warmup + duration,
            baseline_count=baseline_services,
            pool=pool,
        )

    churn_thread = threading.Thread(target=churn_worker)
    churn_thread.start()

    # Warmup
    log.info("Warming up for %.0fs", warmup)
    time.sleep(warmup)

    # Get current service names as fallback; live pool is the primary source.
    service_names = pool.snapshot() or get_active_service_names(namespace)
    if not service_names:
        log.warning("No active services found, using placeholder")
        service_names = ["kubernetes"]

    # Start metrics collection in background
    metrics_result = {}

    def metrics_worker():
        metrics_result["data"] = collect_pod_resources(duration=duration)

    metrics_thread = threading.Thread(target=metrics_worker)
    metrics_thread.start()

    # Run queries via NodePort
    query_results = run_queries(
        server=DNS_HOST,
        port=DNS_PORT,
        service_names=service_names,
        namespace=namespace,
        domain=domain,
        query_rate=query_rate,
        duration=duration,
        tcp_port=DNS_TCP_PORT,
        name_provider=pool.random_name,
    )

    # Wait for threads
    churn_thread.join()
    metrics_thread.join()

    # Scrape plugin Prometheus metrics (cache hits/misses, sign duration, singleflight)
    signing_metrics = scrape_signing_metrics(host=DNS_HOST, port=30153)

    # Scrape standard CoreDNS Prometheus metrics (request duration, response counts)
    coredns_metrics = scrape_coredns_metrics(host=DNS_HOST, port=30153)

    # Collect container-level CPU and memory via kubectl top
    container_resources = collect_container_resources()

    # Save
    meta = {
        "algorithm": algo_name,
        "algorithm_id": algo_id,
        "churn_rate": churn_rate,
        "query_rate": query_rate,
        "duration": duration,
        "warmup": warmup,
        "baseline_services": baseline_services,
        "cache_ttl": cache_ttl,
        "sig_cache_cap": sig_cache_cap,
        "simulated_delay_ms": sim_delay_ms,
        "simulated_stddev_ms": sim_stddev_ms,
        "network_delay_ms": network_delay_ms,
        "network_rate_kbps": network_rate_kbps,
        "total_queries": len(query_results),
        "errors": sum(1 for r in query_results if r.error),
        "churn_created": (
            churn_thread_result.get("state", {}).created_total
            if hasattr(churn_thread_result.get("state"), "created_total")
            else 0
        ),
        "signing_metrics": signing_metrics,
        "coredns_metrics": coredns_metrics,
        "container_resources": container_resources,
    }

    save_results(output_dir, query_results, run_meta=meta)

    # Thorough cleanup: delete services and verify they're gone
    cleanup(namespace)
    _verify_cleanup(namespace)

    log.info("Results saved to %s", output_dir)


def _verify_cleanup(namespace: str, max_retries: int = 3, delay: float = 5.0):
    """Verify all bench services are deleted; retry cleanup if not."""
    for attempt in range(max_retries):
        remaining = get_active_service_names(namespace)
        if not remaining:
            return
        log.warning(
            "Cleanup incomplete: %d services remain (attempt %d/%d)",
            len(remaining),
            attempt + 1,
            max_retries,
        )
        cleanup(namespace)
        time.sleep(delay)
    remaining = get_active_service_names(namespace)
    if remaining:
        log.error("Could not fully clean up %d services — forcing delete", len(remaining))
        subprocess.run(
            [
                "kubectl",
                "-n",
                namespace,
                "delete",
                "svc",
                "-l",
                "bench=churn",
                "--grace-period=0",
                "--force",
            ],
            capture_output=True,
            check=False,
        )
        time.sleep(delay)


def _compact_etcd():
    """Trigger etcd compaction + defrag inside the Kind control-plane container.

    This reclaims memory from deleted Kubernetes objects (services, endpoints, etc.)
    that accumulate during benchmark runs.
    """
    log.info("Compacting etcd to reclaim memory...")
    try:
        # Get etcd pod name
        result = subprocess.run(
            [
                "kubectl",
                "-n",
                "kube-system",
                "get",
                "pods",
                "-l",
                "component=etcd",
                "-o",
                "jsonpath={.items[0].metadata.name}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        etcd_pod = result.stdout.strip()
        if not etcd_pod:
            log.warning("No etcd pod found, skipping compaction")
            return

        # Get current revision
        rev_result = subprocess.run(
            [
                "kubectl",
                "-n",
                "kube-system",
                "exec",
                etcd_pod,
                "--",
                "etcdctl",
                "--endpoints=https://127.0.0.1:2379",
                "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
                "--cert=/etc/kubernetes/pki/etcd/server.crt",
                "--key=/etc/kubernetes/pki/etcd/server.key",
                "endpoint",
                "status",
                "--write-out=fields",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        revision = None
        for line in rev_result.stdout.splitlines():
            if '"Revision"' in line:
                revision = line.split(":")[-1].strip()
                break
        if not revision:
            log.warning("Could not get etcd revision, skipping compaction")
            return

        # Compact
        subprocess.run(
            [
                "kubectl",
                "-n",
                "kube-system",
                "exec",
                etcd_pod,
                "--",
                "etcdctl",
                "--endpoints=https://127.0.0.1:2379",
                "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
                "--cert=/etc/kubernetes/pki/etcd/server.crt",
                "--key=/etc/kubernetes/pki/etcd/server.key",
                "compact",
                revision,
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        # Defrag to release memory back to OS
        subprocess.run(
            [
                "kubectl",
                "-n",
                "kube-system",
                "exec",
                etcd_pod,
                "--",
                "etcdctl",
                "--endpoints=https://127.0.0.1:2379",
                "--cacert=/etc/kubernetes/pki/etcd/ca.crt",
                "--cert=/etc/kubernetes/pki/etcd/server.crt",
                "--key=/etc/kubernetes/pki/etcd/server.key",
                "defrag",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        log.info("etcd compaction + defrag complete")
        time.sleep(5)  # let etcd stabilize
    except Exception as e:
        log.warning("etcd compaction failed (non-fatal): %s", e)


def _restart_control_plane():
    """Restart the Kind control-plane Docker container to fully reclaim memory.

    This is the nuclear option: kills etcd, apiserver, etc. and lets them
    restart fresh. Takes ~30s but reclaims all accumulated memory.
    """
    cp_container = f"{KIND_CLUSTER_NAME}-control-plane"
    log.info("Restarting control-plane container to reclaim memory...")
    subprocess.run(["docker", "restart", cp_container], capture_output=True, timeout=60)
    # Wait for API server to come back
    for _ in range(12):
        time.sleep(5)
        result = subprocess.run(
            ["kubectl", "get", "nodes", "--request-timeout=5s"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log.info("Control-plane restarted and healthy")
            return
    raise RuntimeError("Control-plane failed to recover after restart")


def _restart_worker():
    """Restart the Kind worker Docker container to reclaim memory.

    CoreDNS runs on the worker; after many rapid redeployments the worker's
    memory fragments and old pods linger, blocking new ones.  Restarting the
    container forces kubelet to start fresh and release all accumulated memory.
    """
    worker_container = f"{KIND_CLUSTER_NAME}-worker"
    log.info("Restarting worker container to reclaim memory...")
    subprocess.run(["docker", "restart", worker_container], capture_output=True, timeout=60)
    # Wait for the worker node to become Ready
    for attempt in range(18):
        time.sleep(5)
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "node",
                worker_container,
                "-o",
                "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
                "--request-timeout=5s",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip() == "True":
            log.info("Worker restarted and Ready (attempt %d)", attempt + 1)
            return
    raise RuntimeError("Worker failed to become Ready after restart")


def _drop_caches():
    """Reclaim memory between runs without requiring root.

    Uses Python gc + malloc_trim to release interpreter memory.
    Kernel page cache drops are skipped (require root).
    """
    import gc
    import ctypes

    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def _build_specs(
    algorithms,
    sig_cache_caps,
    cache_ttls,
    sim_delays,
    sim_stddev_ms,
    churn_rates,
    query_rates,
    repetitions,
    results_base,
    network_delays=None,
    network_rates=None,
):
    """Build a flat list of run-spec tuples from the parameter grid.

    Returns list of (algo, scc, ttl, sdel, sim_std, cr, qr, ndel, nrate, rep, out_dir).
    *network_delays* is a list of one-way delay values in ms (or [None]).
    *network_rates* is a list of bandwidth limits in kbps (or [None]; 0/None = unlimited).
    """
    if network_delays is None:
        network_delays = [None]
    if network_rates is None:
        network_rates = [None]
    include_sc = len(sig_cache_caps) > 1
    include_ttl = len(cache_ttls) > 1
    include_ndel = any(d is not None for d in network_delays) and len(network_delays) > 1
    include_nrate = any(r is not None and r > 0 for r in network_rates)
    specs = []
    for algo in algorithms:
        for scc in sig_cache_caps:
            for ttl in cache_ttls:
                for sdel in sim_delays:
                    for ndel in network_delays:
                        for nrate in network_rates:
                            for cr in churn_rates:
                                for qr in query_rates:
                                    for rep in range(repetitions):
                                        parts = []
                                        if include_sc:
                                            parts.append(f"sc{scc if scc is not None else 'def'}")
                                        if include_ttl:
                                            parts.append(f"ttl{ttl}")
                                        if sdel is not None:
                                            parts.append(f"sd{sdel}")
                                        if include_ndel and ndel is not None:
                                            parts.append(f"nd{ndel}")
                                        if include_nrate and nrate is not None and nrate > 0:
                                            parts.append(f"nr{nrate}")
                                        parts.append(f"cr{cr}_qr{int(qr)}")
                                        param_dir = "_".join(parts)
                                        out = (
                                            results_base / algo["label"] / param_dir / f"run_{rep}"
                                        )
                                        specs.append(
                                            (
                                                algo,
                                                scc,
                                                int(ttl),
                                                sdel,
                                                sim_stddev_ms if sdel is not None else None,
                                                cr,
                                                qr,
                                                ndel,
                                                nrate if nrate and nrate > 0 else None,
                                                rep,
                                                out,
                                            )
                                        )
    return specs


def run_campaign(
    config: dict,
    pilot: bool = False,
    short: bool = False,
    smoke: bool = False,
    high_sigma: bool = False,
    sigma_sweep: bool = False,
    simulated_sweep: bool = False,
    netem_sweep: bool = False,
    validation: bool = False,
    resume: bool = False,
    campaign_id: str | None = None,
):
    """Run the full benchmark campaign (or pilot/short/smoke/high_sigma subset).

    Results are stored under ``<output_dir>/<campaign_id>/...``.
    If *campaign_id* is not provided, a UTC timestamp is generated
    automatically (e.g. ``20250716T143022Z``).

    Any section may contain a nested ``simulated:`` block that adds
    simulated-delay runs to the same campaign automatically.
    """
    if campaign_id is None:
        campaign_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if smoke:
        section = config["smoke"]
    elif pilot:
        section = config["pilot"]
    elif short:
        section = config["short"]
    elif validation:
        section = config["validation"]
    elif high_sigma:
        section = config["high_sigma"]
    elif sigma_sweep:
        section = config["sigma_sweep"]
    elif simulated_sweep:
        section = config["simulated_sweep"]
    elif netem_sweep:
        section = config["netem_sweep"]
    else:
        section = config["benchmark"]
    results_base = REPO_DIR / config["results"]["output_dir"] / campaign_id
    log.info("Campaign ID: %s  →  %s", campaign_id, results_base)

    if smoke or pilot or simulated_sweep:
        algorithms = [a for a in config["algorithms"] if a["name"] in section["algorithms"]]
    elif netem_sweep and "algorithms" in section:
        algorithms = [a for a in config["algorithms"] if a["name"] in section["algorithms"]]
    else:
        algorithms = config["algorithms"]

    churn_rates = section["churn_rates"]
    query_rates = section["query_rates"]
    duration = section["run_duration"]
    warmup = section.get("warmup", config["benchmark"].get("warmup", 10))
    baseline = section.get("baseline_services", config["benchmark"]["baseline_services"])
    repetitions = section["repetitions"]
    namespace = config["cluster"]["service_namespace"]
    domain = config["cluster"]["dns_domain"]

    # TTL sweep: high_sigma section may list multiple TTLs
    cache_ttls = section.get("cache_ttls", [config["benchmark"]["cache_ttl"]])
    if isinstance(cache_ttls, (int, float)):
        cache_ttls = [cache_ttls]

    # Signature cache capacity sweep (plugin-level cache)
    sig_cache_caps = section.get("sig_cache_caps", [None])
    if isinstance(sig_cache_caps, (int, float)):
        sig_cache_caps = [sig_cache_caps]

    # Simulated signing delay sweep (ms) — top-level (for standalone simulated_sweep)
    sim_delays = section.get("simulated_delays_ms", [None])
    if isinstance(sim_delays, (int, float)):
        sim_delays = [sim_delays]
    sim_stddev_ms = section.get("simulated_stddev_ms", 0)

    # Network emulation delay sweep (ms) — for netem_sweep campaign
    network_delays = section.get("network_delays_ms", [None])
    if isinstance(network_delays, (int, float)):
        network_delays = [network_delays]

    # Network bandwidth limit sweep (kbps) — 0 or None = unlimited
    network_rates = section.get("network_rates_kbps", [None])
    if isinstance(network_rates, (int, float)):
        network_rates = [network_rates]

    # ── Build flat spec list: main grid ─────────────────────────────────
    specs = _build_specs(
        algorithms,
        sig_cache_caps,
        cache_ttls,
        sim_delays,
        sim_stddev_ms,
        churn_rates,
        query_rates,
        repetitions,
        results_base,
        network_delays=network_delays,
        network_rates=network_rates,
    )
    n_main = len(specs)

    # ── Append specs from nested ``simulated:`` sub-section ─────────────
    sim_section = section.get("simulated")
    if sim_section:
        sim_algos = [a for a in config["algorithms"] if a["name"] in sim_section["algorithms"]]
        sim_sccs = sim_section.get("sig_cache_caps", sig_cache_caps)
        if isinstance(sim_sccs, (int, float)):
            sim_sccs = [sim_sccs]
        sim_ttls = sim_section.get("cache_ttls", cache_ttls)
        if isinstance(sim_ttls, (int, float)):
            sim_ttls = [sim_ttls]
        sim_dels = sim_section["delays_ms"]
        sim_std = sim_section.get("stddev_ms", 0)
        specs += _build_specs(
            sim_algos,
            sim_sccs,
            sim_ttls,
            sim_dels,
            sim_std,
            churn_rates,
            query_rates,
            repetitions,
            results_base,
        )
    n_sim = len(specs) - n_main

    # ── Append specs from nested ``bandwidth:`` sub-section ─────────────
    bw_section = section.get("bandwidth")
    if bw_section:
        bw_algos = [
            a
            for a in config["algorithms"]
            if a["name"] in bw_section.get("algorithms", [a["name"] for a in algorithms])
        ]
        bw_sccs = bw_section.get("sig_cache_caps", sig_cache_caps)
        if isinstance(bw_sccs, (int, float)):
            bw_sccs = [bw_sccs]
        bw_ttls = bw_section.get("cache_ttls", cache_ttls)
        if isinstance(bw_ttls, (int, float)):
            bw_ttls = [bw_ttls]
        bw_delays = bw_section.get("network_delays_ms", [50])
        if isinstance(bw_delays, (int, float)):
            bw_delays = [bw_delays]
        bw_rates = bw_section["network_rates_kbps"]
        if isinstance(bw_rates, (int, float)):
            bw_rates = [bw_rates]
        bw_crs = bw_section.get("churn_rates", churn_rates)
        if isinstance(bw_crs, (int, float)):
            bw_crs = [bw_crs]
        bw_qrs = bw_section.get("query_rates", query_rates)
        if isinstance(bw_qrs, (int, float)):
            bw_qrs = [bw_qrs]
        specs += _build_specs(
            bw_algos,
            bw_sccs,
            bw_ttls,
            [None],
            0,
            bw_crs,
            bw_qrs,
            repetitions,
            results_base,
            network_delays=bw_delays,
            network_rates=bw_rates,
        )
    n_bw = len(specs) - n_main - n_sim

    total = len(specs)
    log.info(
        "Campaign: %d runs (main: %d, simulated: %d, bandwidth: %d)", total, n_main, n_sim, n_bw
    )

    # ── Execute runs ────────────────────────────────────────────────────
    run_idx = 0
    skipped = 0
    prev_deploy_key = None  # (algo_name, cache_ttl, sig_cache_cap, sim_delay) — redeploy when server config changes
    prev_algo = None  # restart CP only when algorithm changes
    prev_netem = object()  # sentinel: track (ndel, nrate) to avoid redundant tc calls
    global_runs_since_compact = 0  # NOT reset on deploy, only on compact/restart

    for algo, scc, ttl, sdel, sim_std, cr, qr, ndel, nrate, rep, out in specs:
        run_idx += 1

        # Resume: skip if results already exist
        if resume and (out / "meta.json").exists():
            skipped += 1
            log.info("=== Run %d/%d === SKIPPED (results exist)", run_idx, total)
            continue

        # Apply/remove netem when network conditions change
        netem_key = (ndel, nrate)
        if netem_key != prev_netem:
            _apply_netem(ndel, rate_kbps=nrate)
            prev_netem = netem_key

        # Restart CP + worker when algorithm changes to reclaim memory
        if algo["name"] != prev_algo:
            if prev_algo is not None:
                _restart_worker()
                _restart_control_plane()
                global_runs_since_compact = 0
            prev_algo = algo["name"]

        # Deploy CoreDNS when server config changes (algo, TTL, sig_cache, sim_delay)
        # Churn rate and query rate are client-side — no redeploy needed
        deploy_key = (algo["name"], ttl, scc, sdel)
        need_deploy = deploy_key != prev_deploy_key
        if need_deploy:
            prev_deploy_key = deploy_key

        # Compact etcd periodically to reclaim memory from churn.
        # Uses absolute run count (not reset on deploy) to ensure
        # compaction actually fires even when deploy groups are small.
        if global_runs_since_compact >= COMPACT_EVERY_N_RUNS:
            _compact_etcd()
            global_runs_since_compact = 0

        # Safety checks
        _drop_caches()
        check_system_resources()
        ensure_cluster_healthy()
        log.info(
            "=== Run %d/%d (sc=%s ttl=%d cr=%.1f qr=%.0f%s%s%s)%s ===",
            run_idx,
            total,
            scc,
            ttl,
            cr,
            qr,
            f" sd={sdel}" if sdel is not None else "",
            f" nd={ndel}" if ndel is not None else "",
            f" nr={nrate}" if nrate is not None else "",
            " [deploy]" if need_deploy else "",
        )
        try:
            run_single(
                algo_name=algo["name"],
                algo_id=algo["id"],
                churn_rate=cr,
                query_rate=qr,
                duration=duration,
                warmup=warmup,
                baseline_services=baseline,
                cache_ttl=ttl,
                namespace=namespace,
                domain=domain,
                output_dir=out,
                algo_type=algo.get("type", "pqc"),
                sig_cache_cap=scc,
                skip_deploy=not need_deploy,
                sim_delay_ms=sdel,
                sim_stddev_ms=sim_std,
                network_delay_ms=ndel,
                network_rate_kbps=nrate,
            )
        except MemoryError:
            raise
        except Exception as e:
            log.warning("Run %d/%d failed: %s — restarting worker and retrying", run_idx, total, e)
            try:
                cleanup(namespace)
                _verify_cleanup(namespace)
            except Exception:
                pass
            try:
                _restart_worker()
                _restart_control_plane()
                global_runs_since_compact = 0
                prev_deploy_key = None  # force redeploy after restart
                run_single(
                    algo_name=algo["name"],
                    algo_id=algo["id"],
                    churn_rate=cr,
                    query_rate=qr,
                    duration=duration,
                    warmup=warmup,
                    baseline_services=baseline,
                    cache_ttl=ttl,
                    namespace=namespace,
                    domain=domain,
                    output_dir=out,
                    algo_type=algo.get("type", "pqc"),
                    sig_cache_cap=scc,
                    skip_deploy=False,
                    sim_delay_ms=sdel,
                    sim_stddev_ms=sim_std,
                    network_delay_ms=ndel,
                    network_rate_kbps=nrate,
                )
            except MemoryError:
                raise
            except Exception as e2:
                log.error("Run %d/%d FAILED after retry: %s", run_idx, total, e2)
                try:
                    cleanup(namespace)
                    _verify_cleanup(namespace)
                except Exception:
                    pass
        global_runs_since_compact += 1
        # Cooldown between runs
        if run_idx < total:
            log.info("Cooldown %ds before next run...", RUN_COOLDOWN)
            time.sleep(RUN_COOLDOWN)

    if skipped:
        log.info("Skipped %d runs with existing results", skipped)

    # Clean up netem rules at campaign end
    has_netem = any(d is not None and d > 0 for d in network_delays) or any(
        r is not None and r > 0 for r in network_rates
    )
    if has_netem:
        _apply_netem(None)
        log.info("Removed netem rules at campaign end")

    return campaign_id

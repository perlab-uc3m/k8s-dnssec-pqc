"""Metrics collector: CPU, memory, signing metrics, and container
resource usage from the CoreDNS pod."""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field

from kubernetes import client, config

log = logging.getLogger(__name__)


@dataclass
class PodMetrics:
    timestamps: list[float] = field(default_factory=list)
    cpu_usage_nanocores: list[int] = field(default_factory=list)
    memory_bytes: list[int] = field(default_factory=list)


def get_coredns_pod_name(api: client.CoreV1Api, namespace: str = "kube-system") -> str:
    pods = api.list_namespaced_pod(namespace=namespace, label_selector="app=coredns-pqc")
    if not pods.items:
        raise RuntimeError("No coredns-pqc pod found")
    return pods.items[0].metadata.name


def scrape_prometheus(pod_ip: str, port: int = 9153) -> dict:
    """Scrape CoreDNS Prometheus metrics endpoint."""
    import urllib.request

    url = f"http://{pod_ip}:{port}/metrics"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            text = resp.read().decode()
        metrics = {}
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2:
                metrics[parts[0]] = parts[1]
        return metrics
    except Exception as e:
        log.warning("Failed to scrape metrics: %s", e)
        return {}


def scrape_signing_metrics(host: str = "127.0.0.1", port: int = 30153) -> dict:
    """Scrape plugin-specific Prometheus metrics via NodePort."""
    import urllib.request

    url = f"http://{host}:{port}/metrics"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            text = resp.read().decode()
    except Exception as e:
        log.warning("Failed to scrape signing metrics from %s: %s", url, e)
        return {}

    result = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        # Parse metric lines like: metric_name{labels} value
        # or: metric_name value
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        key_raw, val_str = parts
        # Strip label portion for matching
        key_base = key_raw.split("{")[0]

        try:
            val = float(val_str)
        except ValueError:
            continue

        if key_base == "coredns_dnssec_pqc_cache_hits_total":
            result["cache_hits"] = result.get("cache_hits", 0) + val
        elif key_base == "coredns_dnssec_pqc_cache_misses_total":
            result["cache_misses"] = result.get("cache_misses", 0) + val
        elif key_base == "coredns_dnssec_pqc_cache_entries":
            result["cache_entries"] = val
        elif key_base == "coredns_dnssec_pqc_singleflight_coalesced_total":
            result["singleflight_coalesced"] = result.get("singleflight_coalesced", 0) + val
        elif key_base == "coredns_dnssec_pqc_singleflight_execs_total":
            result["singleflight_execs"] = result.get("singleflight_execs", 0) + val
        elif key_base == "coredns_dnssec_pqc_sign_duration_seconds_sum":
            result["sign_duration_sum"] = result.get("sign_duration_sum", 0) + val
        elif key_base == "coredns_dnssec_pqc_sign_duration_seconds_count":
            result["sign_duration_count"] = result.get("sign_duration_count", 0) + val

    # Compute mean signing time if available
    if result.get("sign_duration_count", 0) > 0:
        result["sign_duration_mean_ms"] = (
            result["sign_duration_sum"] / result["sign_duration_count"] * 1000
        )

    # Compute cache hit rate
    total = result.get("cache_hits", 0) + result.get("cache_misses", 0)
    if total > 0:
        result["cache_hit_rate"] = result.get("cache_hits", 0) / total

    return result


def scrape_coredns_metrics(host: str = "127.0.0.1", port: int = 30153) -> dict:
    """Scrape standard CoreDNS Prometheus metrics via NodePort."""
    import urllib.request

    url = f"http://{host}:{port}/metrics"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            text = resp.read().decode()
    except Exception as e:
        log.warning("Failed to scrape CoreDNS metrics from %s: %s", url, e)
        return {}

    result: dict = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        key_raw, val_str = parts
        key_base = key_raw.split("{")[0]

        try:
            val = float(val_str)
        except ValueError:
            continue

        # Server-side request duration histogram
        if key_base == "coredns_dns_request_duration_seconds_sum":
            result["request_duration_sum"] = result.get("request_duration_sum", 0) + val
        elif key_base == "coredns_dns_request_duration_seconds_count":
            result["request_duration_count"] = result.get("request_duration_count", 0) + val
        # Request totals by rcode
        elif key_base == "coredns_dns_requests_total":
            result["requests_total"] = result.get("requests_total", 0) + val
        elif key_base == "coredns_dns_responses_total":
            # Extract rcode label
            if "rcode=" in key_raw:
                rcode = (
                    key_raw.split('rcode="')[1].split('"')[0]
                    if 'rcode="' in key_raw
                    else "UNKNOWN"
                )
                rcode_key = f"responses_{rcode}"
                result[rcode_key] = result.get(rcode_key, 0) + val
            result["responses_total"] = result.get("responses_total", 0) + val
        # CoreDNS cache plugin stats
        elif key_base == "coredns_cache_hits_total":
            result["dns_cache_hits"] = result.get("dns_cache_hits", 0) + val
        elif key_base == "coredns_cache_misses_total":
            result["dns_cache_misses"] = result.get("dns_cache_misses", 0) + val
        elif key_base == "coredns_cache_entries":
            result["dns_cache_entries"] = result.get("dns_cache_entries", 0) + val

    # Compute server-side mean request duration
    if result.get("request_duration_count", 0) > 0:
        result["request_duration_mean_ms"] = (
            result["request_duration_sum"] / result["request_duration_count"] * 1000
        )

    return result


def collect_container_resources(
    namespace: str = "kube-system",
    label: str = "app=coredns-pqc",
    samples: int = 3,
    interval: float = 2.0,
) -> dict:
    """Collect container CPU and memory via kubectl top pod."""
    cpu_samples = []
    mem_samples = []

    for i in range(samples):
        if i > 0:
            time.sleep(interval)
        try:
            result = subprocess.run(
                ["kubectl", "top", "pod", "-n", namespace, "-l", label, "--no-headers"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                log.debug("kubectl top failed: %s", result.stderr.strip())
                continue

            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) >= 3:
                    # Parse CPU: "42m" → 42 millicores
                    cpu_str = parts[1]
                    if cpu_str.endswith("m"):
                        cpu_samples.append(int(cpu_str[:-1]))
                    elif cpu_str.endswith("n"):
                        cpu_samples.append(int(cpu_str[:-1]) / 1_000_000)
                    else:
                        cpu_samples.append(int(cpu_str) * 1000)  # whole cores
                    # Parse memory: "64Mi" → 64 MiB
                    mem_str = parts[2]
                    if mem_str.endswith("Mi"):
                        mem_samples.append(float(mem_str[:-2]))
                    elif mem_str.endswith("Ki"):
                        mem_samples.append(float(mem_str[:-2]) / 1024)
                    elif mem_str.endswith("Gi"):
                        mem_samples.append(float(mem_str[:-2]) * 1024)
                    else:
                        mem_samples.append(float(mem_str))
        except Exception as e:
            log.debug("kubectl top sample %d failed: %s", i, e)

    if not cpu_samples:
        return {}

    return {
        "cpu_millicores_mean": sum(cpu_samples) / len(cpu_samples),
        "cpu_millicores_max": max(cpu_samples),
        "cpu_millicores_samples": cpu_samples,
        "memory_mib_mean": sum(mem_samples) / len(mem_samples) if mem_samples else 0,
        "memory_mib_max": max(mem_samples) if mem_samples else 0,
        "memory_mib_samples": mem_samples,
        "n_samples": len(cpu_samples),
    }


def collect_pod_resources(
    namespace: str = "kube-system",
    interval: float = 1.0,
    duration: float = 60.0,
    kubeconfig: str | None = None,
) -> PodMetrics:
    """Poll pod resource usage at regular intervals."""
    if kubeconfig:
        config.load_kube_config(config_file=kubeconfig)
    else:
        config.load_kube_config()

    api = client.CoreV1Api()
    pod_name = get_coredns_pod_name(api, namespace)

    # Use exec to read /proc stats from inside the container
    metrics = PodMetrics()
    start = time.monotonic()

    while time.monotonic() - start < duration:
        t = time.monotonic()
        try:
            from kubernetes.stream import stream

            resp = stream(
                api.connect_get_namespaced_pod_exec,
                pod_name,
                namespace,
                command=["cat", "/proc/1/status"],
                stdout=True,
                stderr=False,
                stdin=False,
                tty=False,
            )
            for line in resp.splitlines():
                if line.startswith("VmRSS:"):
                    kb = int(line.split()[1])
                    metrics.memory_bytes.append(kb * 1024)
                    metrics.timestamps.append(t)
        except Exception as e:
            log.debug("Resource poll failed: %s", e)

        elapsed = time.monotonic() - t
        time.sleep(max(0, interval - elapsed))

    return metrics

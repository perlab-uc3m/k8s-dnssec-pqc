"""Offline analyses behind the final manuscript: signing model, CPU per load, latency stages.

Everything here reads saved run artifacts only. Nothing selects runs; every function
consumes all complete repetitions of the requested cells.
"""

from __future__ import annotations

import gzip
import json
import math
from pathlib import Path

import numpy as np


def run_dirs(campaign: Path, cell: str) -> list[Path]:
    runs = sorted(p.parent for p in campaign.glob(f"runs/{cell}/rep-*/attempt-*/COMPLETE"))
    if not runs:
        raise ValueError(f"No complete runs for {cell}")
    return runs


def _queries(run: Path) -> list[dict]:
    with gzip.open(run / "queries.jsonl.gz", "rt") as file:
        return [json.loads(line) for line in file]


def offered_integral(mode: str, rate: float, duration: float, t0: float, t1: float) -> float:
    """Expected offered queries in [t0, t1) for the generator's arrival profile."""
    t0, t1 = max(0.0, t0), min(duration, t1)
    if t1 <= t0:
        return 0.0
    if mode == "poisson":
        return rate * (t1 - t0)
    if mode != "burst_2_of_20":
        raise ValueError(f"Unknown arrival mode: {mode}")
    high_s = int(duration // 20.0) * 2.0 + min(2.0, duration % 20.0)
    low = rate / (1.0 + 9.0 * high_s / duration)
    total, start = 0.0, 0.0
    while start < duration:
        for a, b, r in ((start, start + 2.0, 10.0 * low), (start + 2.0, start + 20.0, low)):
            lo, hi = max(a, t0), min(b, t1, duration)
            if hi > lo:
                total += (hi - lo) * r
        start += 20.0
    return total


def popularity_weights(popularity: str, names: int) -> list[float]:
    if popularity == "uniform":
        return [1.0 / names] * names
    if popularity != "zipf_1_1":
        raise ValueError(f"Unknown popularity: {popularity}")
    weights = [1 / ((i + 1) ** 1.1) for i in range(names)]
    total = sum(weights)
    return [w / total for w in weights]


def predicted_signs(run: Path) -> dict:
    """Signing operations expected from the realized update schedule (cached cells).

    A new record version is signed by a pod when that pod receives its first lookup
    for the name before the name changes again or the window ends. Queries are a
    Poisson process with the configured profile and popularity. With R replicas and
    k DNS transactions per query, a query reaches a given pod with probability
    1 - (1 - 1/R)^k.
    """
    config = json.loads((run / "config.json").read_text())
    load = json.loads((run / "load.json").read_text())
    summary = json.loads((run / "summary.json").read_text())
    if config["signature_cache_capacity"] == 0 or config["algorithm_id"] == 0:
        raise ValueError("The cache model applies to signed runs with a signature cache")
    duration = load["duration_s"]
    events = [
        json.loads(line)
        for line in (run / "updates.jsonl").read_text().splitlines()
        if line.strip()
    ]
    events = [e for e in events if e.get("dispatched", True) and e.get("error") is None]
    names = config["names"]
    weights = popularity_weights(config["popularity"], names)
    replicas = config.get("replicas", 1)
    per_query = summary["dns_transactions"] / summary["offered"]
    reach = 1.0 - (1.0 - 1.0 / replicas) ** per_query
    expected = 0.0
    for index, event in enumerate(events):
        following = next(
            (e["requested_s"] for e in events[index + 1 :] if e["name"] == event["name"]),
            duration,
        )
        offered = offered_integral(
            config["arrival_mode"],
            config["query_rate"],
            duration,
            event["acknowledged_s"],
            following,
        )
        mean = offered * weights[event["index"]] * reach
        expected += replicas * -math.expm1(-mean)
    rate, window = summary["signs_per_s"], summary["cpu_window_min_s"]
    measured = rate * window if rate is not None and window is not None else None
    return {
        "cell_id": config["cell_id"],
        "repetition": config["repetition"],
        "updates": len(events),
        "predicted": expected,
        "measured": round(measured) if measured is not None else None,
    }


def stationary_signs_per_s(
    query_rate: float, update_rate: float, weights: list[float], replicas: int = 1, reach=1.0
) -> float:
    """Equation 1 summed over names: r = sum_i q_i u_i / (q_i + u_i), per pod."""
    u = update_rate / len(weights)
    total = 0.0
    for w in weights:
        q = query_rate * w * reach
        total += q * u / (q + u)
    return replicas * total


def cpu_per_second(run: Path, kind: str) -> list[tuple[float, float]]:
    """(offered queries/s, CPU cores) for each sampling interval of one run.

    kind is "dns" (CoreDNS process counter, 10 ms ticks) or "client" (generator rusage).
    """
    load = json.loads((run / "load.json").read_text())
    anchor = load["monotonic_anchor"]
    planned = np.sort([q["planned_s"] for q in _queries(run)])
    series: dict[str, list[tuple[float, float]]] = {}
    if kind == "dns":
        for line in (run / "metrics.jsonl").read_text().splitlines():
            row = json.loads(line)
            value = row.get("series", {}).get("process_cpu_seconds_total")
            if value is not None and row["phase"] in (
                "measurement_start",
                "measurement",
                "measurement_end",
            ):
                series.setdefault(row["uid"], []).append((row["monotonic_s"] - anchor, value))
    elif kind == "client":
        for line in (run / "host_metrics.jsonl").read_text().splitlines():
            row = json.loads(line)
            if "client_cpu_s" in row:
                series.setdefault("client", []).append(
                    (row["monotonic_s"] - anchor, row["client_cpu_s"])
                )
    else:
        raise ValueError(kind)
    if len(series) != 1:
        raise ValueError("Per-interval CPU is defined here for one process")
    samples = next(iter(series.values()))
    out = []
    for (t0, c0), (t1, c1) in zip(samples, samples[1:], strict=False):
        if t1 - t0 < 0.5:
            continue
        count = np.searchsorted(planned, t1) - np.searchsorted(planned, t0)
        out.append((count / (t1 - t0), (c1 - c0) / (t1 - t0)))
    return out


def burst_phase_p99(run: Path) -> dict:
    """P99 client delay before first write and exchange time in high and low phases."""
    rows = [q for q in _queries(run) if q["status"] == "positive_unverified"]
    result = {}
    for phase, test in (("low", lambda t: t % 20.0 >= 2.0), ("high", lambda t: t % 20.0 < 2.0)):
        chosen = [q for q in rows if test(q["planned_s"])]
        result[phase] = {
            "queries": len(chosen),
            "dispatch_p99_ms": float(np.percentile([q["dispatch_lag_ms"] for q in chosen], 99)),
            "exchange_p99_ms": float(np.percentile([q["wire_latency_ms"] for q in chosen], 99)),
        }
    return result


def exchange_times(campaign: Path, cell: str) -> np.ndarray:
    values = [
        q["wire_latency_ms"]
        for run in run_dirs(campaign, cell)
        for q in _queries(run)
        if q["status"] == "positive_unverified"
    ]
    return np.sort(np.asarray(values))


def linear_fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    x = np.array([p[0] for p in points])
    y = np.array([p[1] for p in points])
    slope, intercept = np.polyfit(x, y, 1)
    return float(slope), float(intercept)


def answered_fraction(run: Path, deadline_ms: float) -> float:
    """Offered queries that received a verified (or, unsigned, positive) answer in time."""
    queries = _queries(run)
    verification = run / "verification.jsonl"
    verified = None
    if verification.exists():
        with verification.open() as file:
            verified = {
                row["query_id"]
                for row in map(json.loads, file)
                if row["verification"] == "private_zone_signature_verified"
            }
    good = 0
    for q in queries:
        if q["status"] != "positive_unverified" or q["latency_ms"] is None:
            continue
        if verified is not None and q["query_id"] not in verified:
            continue
        good += q["latency_ms"] <= deadline_ms
    return good / len(queries) if queries else float("nan")


def predicted_cache_rates(summary: dict) -> dict:
    """Stationary per-pod model at the realized update rate, with any response cache.

    Uses the renewal result in freshness.ideal_cache_rates for each name; with no
    response cache it reduces to Eq. 1. Returns total signing rate over pods and the
    stale fraction of answers.
    """
    from .freshness import ideal_cache_rates

    names = summary["names"]
    replicas = summary.get("replicas") or 1
    duration = summary["duration_s"]
    updates = (summary.get("updates_acknowledged") or 0) / duration
    weights = popularity_weights(summary.get("popularity", "uniform"), names)
    per_query = summary["dns_transactions"] / summary["offered"]
    reach = 1.0 - (1.0 - 1.0 / replicas) ** per_query
    ttl = summary.get("response_cache_ttl") or 0
    signs = stale = 0.0
    for w in weights:
        rates = ideal_cache_rates(
            summary["query_rate_configured"] * w * reach, updates / names, ttl
        )
        signs += rates["signs_per_s"]
        stale += w * rates["stale_fraction"]
    return {"signs_per_s": replicas * signs, "signs_per_pod": signs, "stale_fraction": stale}


def algorithm_sizes(run: Path, limit: int = 200) -> dict:
    """Public key bytes from the saved DNSKEY and RRSIG signature bytes from answers."""
    import base64
    import struct

    import dns.message
    import dns.rdatatype

    result: dict = {"public_key": None, "signature_max": None, "signature_median": None}
    anchor = run / "trust_anchor.key"
    if anchor.exists():
        for line in anchor.read_text().splitlines():
            if "DNSKEY" in line and not line.lstrip().startswith(";"):
                fields = line.split()
                i = fields.index("DNSKEY")
                result["public_key"] = len(base64.b64decode("".join(fields[i + 4 :])))
                break
    final: dict[int, bytes] = {}
    with gzip.open(run / "responses.bin.gz", "rb") as file:
        while header := file.read(13):
            query_id, _, length = struct.unpack(">QBI", header)
            final[query_id] = file.read(length)
    lengths = []
    for wire in list(final.values())[:limit]:
        message = dns.message.from_wire(wire, raise_on_truncation=False)
        for rrset in message.answer:
            if rrset.rdtype == dns.rdatatype.RRSIG:
                lengths.extend(len(rr.signature) for rr in rrset)
    if lengths:
        result["signature_max"] = max(lengths)
        result["signature_median"] = float(np.median(lengths))
    return result


def mean_inflight(run: Path) -> float | None:
    """Mean number of signing leaders in flight over the periodic samples of one pod."""
    values = []
    for line in (run / "metrics.jsonl").read_text().splitlines():
        row = json.loads(line)
        if row.get("phase") == "measurement":
            value = row.get("series", {}).get("coredns_dnssec_pqc_sign_inflight")
            if value is not None:
                values.append(value)
    return float(np.mean(values)) if values else None


def latency_quantiles(run: Path, qs=(0.5, 0.99, 0.999)) -> dict:
    """Quantiles of successful-answer latency and the maximum, in ms."""
    values = [
        q["latency_ms"]
        for q in _queries(run)
        if q["status"] == "positive_unverified" and q["latency_ms"] is not None
    ]
    if not values:
        return {**{f"q{q}": None for q in qs}, "max": None}
    return {**{f"q{q}": float(np.quantile(values, q)) for q in qs}, "max": float(max(values))}

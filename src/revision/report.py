"""Offline per-run summaries and figures. Never select a favored repetition."""
from __future__ import annotations

import csv
import gzip
import json
import math
import statistics
import struct
from collections import Counter
from pathlib import Path


def _rows(path: Path):
    with gzip.open(path, "rt") as file:
        for line in file:
            yield json.loads(line)


def _frames(path: Path):
    with gzip.open(path, "rb") as file:
        while header := file.read(13):
            if len(header) != 13:
                raise ValueError(f"Short frame header in {path}")
            query_id, attempt, length = struct.unpack(">QBI", header)
            data = file.read(length)
            if len(data) != length:
                raise ValueError(f"Short DNS frame in {path}")
            yield query_id, attempt, data


def _metric_delta(samples: list[dict], prefix: str) -> tuple[float, float] | None:
    first = next((x for x in samples if x["phase"] == "measurement_start"), None)
    last = next((x for x in reversed(samples) if x["phase"] == "measurement_end"), None)
    if first is None or last is None:
        return None
    a = sum(v for k, v in first.get("series", {}).items() if k.startswith(prefix))
    b = sum(v for k, v in last.get("series", {}).items() if k.startswith(prefix))
    elapsed = last["t_s"] - first["t_s"]
    return (b - a, elapsed) if b >= a and elapsed > 0 else None


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    at = (len(values) - 1) * q
    low, high = int(at), math.ceil(at)
    return values[low] * (high - at) + values[high] * (at - low) if high != low else values[low]


def _visibility(run_dir: Path, observations: dict[str, list[dict]], require_verified: bool) -> dict:
    path = run_dir / "updates.jsonl"
    if not path.exists():
        return {}
    events = [json.loads(line) for line in path.open()]
    acknowledged = [e for e in events if e.get("error") is None]
    visible_delays = []
    stale = 0
    post_ack_observations = 0
    censored = 0
    for index, event in enumerate(acknowledged):
        next_ack = next((other["acknowledged_s"] for other in acknowledged[index + 1:]
                         if other["name"] == event["name"]), float("inf"))
        found = False
        for row in observations.get(event["name"], []):
            sent = row.get("sent_s")
            if sent is None or sent < event["acknowledged_s"] or sent >= next_ack:
                continue
            if require_verified and row.get("_verification") != "private_zone_signature_verified":
                continue
            if row.get("answer_a"):
                post_ack_observations += 1
            if event["expected_a"] in row.get("answer_a", []):
                if not found:
                    visible_delays.append(max(0.0, row["completed_s"] - event["acknowledged_s"]) * 1000)
                    found = True
            elif row.get("answer_a"):
                stale += 1
        if not found:
            censored += 1
    return {
        "updates_acknowledged": len(acknowledged),
        "updates_censored": censored,
        "updates_observed": len(visible_delays),
        "stale_post_ack_answers": stale,
        "post_ack_positive_observations": post_ack_observations,
        "stale_post_ack_fraction": stale / post_ack_observations if post_ack_observations else None,
        "visibility_upper_p50_ms": _percentile(visible_delays, 0.5),
        "visibility_upper_p95_ms": _percentile(visible_delays, 0.95),
    }


def summarize_run(run_dir: Path) -> dict:
    config = json.loads((run_dir / "config.json").read_text())
    load = json.loads((run_dir / "load.json").read_text())
    query_path = run_dir / "queries.jsonl.gz"
    frame_path = run_dir / "responses.bin.gz"
    if not query_path.exists() or not frame_path.exists():
        raise ValueError(f"Missing response artifacts in {run_dir}")
    statuses: Counter[str] = Counter()
    latencies, lags, wire_latencies = [], [], []
    ids, expected_frames = set(), 0
    verification_path = run_dir / "verification.jsonl"
    verification_rows = iter(verification_path.open()) if verification_path.exists() else None
    verified_count = 0
    verified_within_window = 0
    fallback_count = 0
    final_wire_bytes = []
    verified_latencies = []
    observations: dict[str, list[dict]] = {}
    signing_keys: Counter[tuple[str, tuple[str, ...]]] = Counter()
    dns_transactions = 0
    for row in _rows(query_path):
        query_id = row["query_id"]
        if query_id in ids:
            raise ValueError(f"Duplicate query ID in {run_dir}")
        ids.add(query_id)
        statuses[row["status"]] += 1
        observations.setdefault(row["fqdn"], []).append(row)
        dns_transactions += len(row["attempts"])
        if row["answer_a"]:
            signing_keys[(row["fqdn"], tuple(row["answer_a"]))] += len(row["attempts"])
        if verification_rows is not None:
            try:
                verification = json.loads(next(verification_rows))
            except StopIteration as exc:
                raise ValueError(f"Missing verification row in {run_dir}") from exc
            if verification["query_id"] != query_id:
                raise ValueError(f"Verification order mismatch in {run_dir}")
            row["_verification"] = verification["verification"]
            if verification["verification"] == "private_zone_signature_verified":
                verified_count += 1
                verified_latencies.append(row["latency_ms"])
                if row["completed_s"] <= load["duration_s"]:
                    verified_within_window += 1
        if row["attempts"]:
            final_wire_bytes.append(row["attempts"][-1]["wire_bytes"])
        if any(a["transport"] == "tcp" for a in row["attempts"]) and any(
            a["transport"] == "udp" for a in row["attempts"]
        ):
            fallback_count += 1
        expected_frames += len(row["attempts"])
        if row["status"] == "positive_unverified":
            latencies.append(row["latency_ms"])
            wire_latencies.append(row["wire_latency_ms"])
        if row["dispatch_lag_ms"] is not None:
            lags.append(row["dispatch_lag_ms"])
    if verification_rows is not None:
        try:
            next(verification_rows)
            raise ValueError(f"Extra verification row in {run_dir}")
        except StopIteration:
            pass
    if len(ids) != load["offered"] or Counter(load["counts"]) != statuses:
        raise ValueError(f"Query accounting mismatch in {run_dir}")
    frame_ids = [(i, n) for i, n, _ in _frames(frame_path)]
    if len(frame_ids) != expected_frames or any(i not in ids for i, _ in frame_ids):
        raise ValueError(f"Response frame accounting mismatch in {run_dir}")
    samples: list[dict] = []
    metrics_path = run_dir / "metrics.jsonl"
    if metrics_path.exists():
        samples = [json.loads(line) for line in metrics_path.open()]
    by_pod: dict[str, list[dict]] = {}
    for sample in samples:
        by_pod.setdefault(sample["uid"], []).append(sample)
    cpu_seconds = [_metric_delta(s, "process_cpu_seconds_total") for s in by_pod.values()]
    cpu_total = sum(x[0] for x in cpu_seconds) if cpu_seconds and all(x is not None for x in cpu_seconds) else None
    cpu_interval = statistics.mean(x[1] for x in cpu_seconds) if cpu_total is not None else None
    sign_total = [
        _metric_delta(s, "coredns_dnssec_pqc_singleflight_execs_total")
        for s in by_pod.values()
    ]
    sign_interval = statistics.mean(x[1] for x in sign_total) if sign_total and all(x is not None for x in sign_total) else None
    sign_total = sum(x[0] for x in sign_total) if sign_interval is not None else None
    def counter(prefix: str) -> float | None:
        deltas = [_metric_delta(pod_samples, prefix) for pod_samples in by_pod.values()]
        return sum(x[0] for x in deltas) if deltas and all(x is not None for x in deltas) else None
    hits = counter("coredns_dnssec_pqc_cache_hits_total")
    misses = counter("coredns_dnssec_pqc_cache_misses_total")
    coalesced = counter("coredns_dnssec_pqc_singleflight_coalesced_total")
    sign_wall_sum = counter("coredns_dnssec_pqc_sign_duration_seconds_sum")
    sign_wall_count = counter("coredns_dnssec_pqc_sign_duration_seconds_count")
    mean_sign_wall_s = sign_wall_sum / sign_wall_count if sign_wall_sum is not None and sign_wall_count and sign_wall_count > 0 else None
    predicted_coalescence = None
    if config.get("signature_cache_capacity") == 0 and config.get("response_cache_ttl") == 0 and mean_sign_wall_s is not None:
        total_calls = sum(signing_keys.values())
        if total_calls:
            predicted_coalescence = sum(
                (calls / total_calls) * ((calls / load["duration_s"] * mean_sign_wall_s) /
                                       (1 + calls / load["duration_s"] * mean_sign_wall_s))
                for calls in signing_keys.values()
            )
    entries = 0.0
    entries_seen = False
    for pod_samples in by_pod.values():
        end = next((x for x in reversed(pod_samples) if x["phase"] == "measurement_end"), None)
        if end is not None:
            values = [v for k, v in end.get("series", {}).items()
                      if k.startswith("coredns_dnssec_pqc_cache_entries{")]
            if values:
                entries += sum(values)
                entries_seen = True
    cgroup_quota = None
    throttled_fraction = None
    throttled_seconds = None
    cgroup_peak = None
    cgroup_path = run_dir / "cgroup.jsonl"
    if cgroup_path.exists():
        cg_rows = [json.loads(line) for line in cgroup_path.open()]
        cg_pods = {}
        for row in cg_rows:
            cg_pods.setdefault(row["uid"], {})[row["phase"]] = row
        if len(cg_pods) == len(by_pod) and all(
            "cgroup" in phases.get("measurement_start", {}) and
            "cgroup" in phases.get("measurement_end", {})
            for phases in cg_pods.values()
        ):
            periods = throttled = usec = 0
            quota = 0.0
            peak = 0
            for phases in cg_pods.values():
                a = phases["measurement_start"]["cgroup"]
                b = phases["measurement_end"]["cgroup"]
                limits = a["cpu.max"].split()
                if limits[0] != "max":
                    quota += int(limits[0]) / int(limits[1])
                sa, sb = a["cpu.stat"], b["cpu.stat"]
                periods += sb["nr_periods"] - sa["nr_periods"]
                throttled += sb["nr_throttled"] - sa["nr_throttled"]
                usec += sb["throttled_usec"] - sa["throttled_usec"]
                peak += b["memory.peak"] if isinstance(b["memory.peak"], int) else 0
            cgroup_quota = quota
            throttled_fraction = throttled / periods if periods > 0 else None
            throttled_seconds = usec / 1e6
            cgroup_peak = peak
    duration = load["duration_s"]
    positive = statuses["positive_unverified"]
    analysis_latencies = verified_latencies if verification_rows is not None else latencies
    accepted_count = verified_count if verification_rows is not None else positive
    accepted_within_window = verified_within_window if verification_rows is not None else None
    rss_peak = None
    if samples:
        rss_values = [v for sample in samples for k, v in sample.get("series", {}).items()
                      if k == "process_resident_memory_bytes"]
        if rss_values:
            rss_peak = max(rss_values)
    return {
        **_visibility(run_dir, observations, verification_rows is not None),
        "run": str(run_dir),
        "cell_id": config["cell_id"],
        "repetition": config["repetition"],
        "algorithm": config["algorithm"],
        "workload": config["workload"],
        "policy": config["transport_policy"],
        "duration_s": duration,
        "offered": len(ids),
        "positive_unverified": positive,
        "verified_positive": verified_count if verification_rows is not None else None,
        "failure_or_unknown": len(ids) - accepted_count,
        "client_rejected": statuses["client_rejected"],
        "timed_out": statuses["timeout"],
        "transport_errors": statuses["transport_error"],
        "offered_qps": len(ids) / duration,
        "positive_unverified_qps": positive / duration,
        "verified_qps": verified_count / duration if verification_rows is not None else None,
        "verified_within_window_qps": accepted_within_window / duration if accepted_within_window is not None else None,
        "fallback_count": fallback_count,
        "fallback_fraction": fallback_count / len(ids) if ids else None,
        "final_wire_p50_bytes": _percentile(final_wire_bytes, 0.5),
        "sampled_rss_peak_bytes": rss_peak,
        "cpu_quota_cores": cgroup_quota,
        "throttled_period_fraction": throttled_fraction,
        "throttled_seconds": throttled_seconds,
        "cgroup_memory_peak_bytes": cgroup_peak,
        "p50_ms": _percentile(analysis_latencies, 0.5),
        "p95_ms": _percentile(analysis_latencies, 0.95),
        "p99_ms": _percentile(analysis_latencies, 0.99),
        "wire_p50_ms": _percentile(wire_latencies, 0.5),
        "wire_p99_ms": _percentile(wire_latencies, 0.99),
        "dispatch_p99_ms": _percentile(lags, 0.99),
        "cpu_cores": cpu_total / cpu_interval if cpu_total is not None else None,
        "signs_per_s": sign_total / sign_interval if sign_total is not None else None,
        "signature_cache_hits": hits,
        "signature_cache_misses": misses,
        "signature_cache_hit_fraction": hits / (hits + misses) if hits is not None and misses is not None and hits + misses > 0 else None,
        "signature_cache_entries_end": entries if entries_seen else None,
        "singleflight_coalesced": coalesced,
        "coalescence_observed": coalesced / (coalesced + sign_total) if coalesced is not None and sign_total is not None and coalesced + sign_total > 0 else None,
        "coalescence_predicted_independent": predicted_coalescence,
        "sign_wall_mean_ms": mean_sign_wall_s * 1000 if mean_sign_wall_s is not None else None,
        "dns_transactions": dns_transactions,
        "approximate_content_keys": len(signing_keys),
        "metrics_pods": len(by_pod),
        "frames": len(frame_ids),
        "statuses": dict(statuses),
        "validation": "private_zone_signature_checked" if verification_rows is not None else "cryptographic_verification_pending",
    }


def report(campaign: Path) -> list[dict]:
    run_dirs = sorted(p.parent for p in campaign.glob(
        "runs/*/rep-*/attempt-*/COMPLETE"
    ))
    if not run_dirs:
        raise ValueError(f"No complete runs under {campaign}")
    summaries = [summarize_run(p) for p in run_dirs]
    for run_dir, summary in zip(run_dirs, summaries):
        (run_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    tables = campaign / "tables"
    figures = campaign / "figures"
    tables.mkdir(exist_ok=True)
    figures.mkdir(exist_ok=True)
    columns = [k for k in summaries[0] if k != "statuses"]
    with (tables / "run_summaries.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for row in summaries:
            writer.writerow({k: row[k] for k in columns})
    (tables / "outcomes.json").write_text(
        json.dumps({row["run"]: row["statuses"] for row in summaries}, indent=2)
    )
    by_cell: dict[str, list[dict]] = {}
    for row in summaries:
        by_cell.setdefault(row["cell_id"], []).append(row)
    cell_rows = []
    for cell_id, runs in sorted(by_cell.items()):
        def med(key: str):
            values = [row[key] for row in runs if row.get(key) is not None]
            return statistics.median(values) if values else None
        cell_rows.append({
            "cell_id": cell_id, "repetitions_complete": len(runs),
            "offered_qps_median": med("offered_qps"),
            "dispatch_p99_ms_median": med("dispatch_p99_ms"),
            "client_rejected_total": sum(row["client_rejected"] for row in runs),
            "verified_fraction_median": statistics.median(
                [row["verified_positive"] / row["offered"] for row in runs]
            ) if all(row["verified_positive"] is not None for row in runs) else None,
            "p99_ms_median": med("p99_ms"),
            "wire_p99_ms_median": med("wire_p99_ms"),
            "p99_ms_min": min((row["p99_ms"] for row in runs if row["p99_ms"] is not None), default=None),
            "p99_ms_max": max((row["p99_ms"] for row in runs if row["p99_ms"] is not None), default=None),
            "cpu_cores_median": med("cpu_cores"),
            "fallback_fraction_median": med("fallback_fraction"),
            "signs_per_s_median": med("signs_per_s"),
            "visibility_upper_p50_ms_median": med("visibility_upper_p50_ms"),
            "stale_post_ack_fraction_median": med("stale_post_ack_fraction"),
        })
    with (tables / "cell_summaries.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(cell_rows[0]))
        writer.writeheader()
        writer.writerows(cell_rows)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    labels = [f'{r["cell_id"]}\nrep {r["repetition"]}' for r in summaries]
    xs = range(len(summaries))
    fig, axes = plt.subplots(2, 1, figsize=(max(8, len(summaries)*0.6), 7), sharex=True)
    axes[0].scatter(xs, [r["p99_ms"] for r in summaries], label="P99 of positive responses")
    axes[0].set_ylabel("Latency (ms)")
    axes[0].legend()
    axes[1].scatter(xs, [r["cpu_cores"] for r in summaries], label="CoreDNS CPU cores")
    axes[1].set_ylabel("CPU cores")
    axes[1].legend()
    axes[1].set_xticks(list(xs), labels, rotation=70, ha="right")
    fig.tight_layout()
    fig.savefig(figures / "latency_cpu.pdf")
    fig.savefig(figures / "latency_cpu.png", dpi=150)
    plt.close(fig)
    selected = [
        ("unsigned-reference", "Unsigned"),
        ("ed25519-reference", "Ed25519"),
        ("falcon512-reference", "Falcon-512"),
        ("mldsa44-reference", "ML-DSA-44"),
        ("mldsa44-no-signature-cache", "ML-DSA-44\nno sig. cache"),
        ("mldsa44-fast-updates", "ML-DSA-44\n10 updates/s"),
    ]
    selected = [(key, label) for key, label in selected if key in by_cell]
    if selected:
        fig, axes = plt.subplots(2, 1, figsize=(8.5, 5.4), sharex=True)
        for x, (key, label) in enumerate(selected):
            runs = sorted(by_cell[key], key=lambda row: row["repetition"])
            for offset, row in enumerate(runs):
                dx = (offset - (len(runs)-1)/2) * 0.075
                for ax, field in ((axes[0], "p99_ms"), (axes[1], "cpu_cores")):
                    if row[field] is not None:
                        ax.scatter(x + dx, row[field], s=24, color="#174a70", alpha=0.78)
            for ax, field in ((axes[0], "p99_ms"), (axes[1], "cpu_cores")):
                values = [row[field] for row in runs if row[field] is not None]
                if values:
                    ax.plot(x, statistics.median(values), marker="_", markersize=17,
                            markeredgewidth=2, color="#b33d32")
        axes[0].set_ylabel("P99 of successful answers (ms)")
        axes[1].set_ylabel("CoreDNS process CPU (cores)")
        axes[1].set_xticks(range(len(selected)), [label for _, label in selected])
        for ax in axes:
            ax.grid(axis="y", alpha=0.2)
        fig.tight_layout()
        fig.savefig(figures / "revision_contrasts.pdf")
        fig.savefig(figures / "revision_contrasts.png", dpi=180)
        plt.close(fig)
    lines = [
        "# Offline campaign report",
        "",
        f"Complete runs: {len(summaries)}.",
        "",
        "Private zone signature verification status is recorded per run. Negative proofs are not validated.",
        "",
        "| Cell | Rep | Offered | Verified | Failure or unknown | P99 ms | CPU cores |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summaries:
        p99 = f'{row["p99_ms"]:.2f}' if row["p99_ms"] is not None else "n/a"
        cpu = f'{row["cpu_cores"]:.3f}' if row["cpu_cores"] is not None else "n/a"
        lines.append(
            f'| {row["cell_id"]} | {row["repetition"]} | {row["offered"]} | '
            f'{row["verified_positive"] if row["verified_positive"] is not None else "n/a"} | {row["failure_or_unknown"]} | {p99} | {cpu} |'
        )
    (campaign / "report.md").write_text("\n".join(lines) + "\n")
    return summaries

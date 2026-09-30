"""Freshness at answer completion, with uncertain updates kept in the denominator."""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections import Counter

VERIFIED = "private_zone_signature_verified"
DEADLINES_MS = (10, 25, 50, 100, 250, 1000)


class Freshness:
    def __init__(self, initial: dict, events: list[dict], signed: bool):
        self.signed = signed
        self.initial = initial
        self.by_name = {}
        for event in events:
            if event.get("dispatched", True):
                self.by_name.setdefault(event["name"], []).append(event)
        self.index = {}
        for name, rows in self.by_name.items():
            rows.sort(key=lambda e: e["requested_s"])
            acks = sorted(
                (e for e in rows if e.get("error") is None), key=lambda e: e["acknowledged_s"]
            )
            versions = {e["expected_a"]: (e["version"], e["requested_s"]) for e in rows}
            self.index[name] = (
                rows,
                [e["requested_s"] for e in rows],
                acks,
                [e["acknowledged_s"] for e in acks],
                versions,
            )
        self.counts = Counter()
        self.on_time = Counter()
        self.uncertain_on_time = Counter()
        self.stale_ages_ms = []

    def classify(self, row: dict) -> tuple[str, float | None]:
        if row["status"] != "positive_unverified" or (
            self.signed and row.get("_verification") != VERIFIED
        ):
            return "failed_or_unverified", None
        name, done = row["fqdn"], row.get("completed_s")
        initial = self.initial.get(name)
        if initial is None or done is None or len(row.get("answer_a", [])) != 1:
            return "unknown", None
        rows, requested, acks, acknowledged, versions = self.index.get(name, ([], [], [], [], {}))
        seen = (
            (initial["version"], -math.inf)
            if row["answer_a"][0] == initial["address"]
            else versions.get(row["answer_a"][0])
        )
        if seen is None or seen[1] > done:
            return "unexpected_address", None
        ack_index = bisect_right(acknowledged, done)
        latest = acks[ack_index - 1] if ack_index else None
        reference = latest["version"] if latest else initial["version"]
        if seen[0] < reference:
            superseding = next((e for e in acks[:ack_index] if e["version"] > seen[0]), latest)
            return "stale", max(0.0, (done - superseding["acknowledged_s"]) * 1000)
        request_index = bisect_right(requested, done)
        last_request = rows[request_index - 1] if request_index else None
        if last_request and (
            last_request.get("error") is not None or last_request["acknowledged_s"] > done
        ):
            return "unknown", None
        return ("fresh", None) if seen[0] == reference else ("unknown", None)

    def observe(self, row: dict) -> dict:
        status, age = self.classify(row)
        self.counts[status] += 1
        latency = row.get("latency_ms")
        for deadline in DEADLINES_MS:
            if latency is not None and latency <= deadline:
                if status == "fresh":
                    self.on_time[deadline] += 1
                elif status == "unknown":
                    self.uncertain_on_time[deadline] += 1
        if age is not None:
            self.stale_ages_ms.append(age)
        return {
            "query_id": row["query_id"],
            "freshness_at_completion": status,
            "stale_after_ack_ms": age,
        }

    def summary(self) -> dict:
        total = sum(self.counts.values())
        result = {
            "freshness_" + key: self.counts[key]
            for key in ("fresh", "stale", "unknown", "unexpected_address", "failed_or_unverified")
        }
        result["stale_after_ack_p50_ms"] = (
            statistics.median(self.stale_ages_ms) if self.stale_ages_ms else None
        )
        result["stale_after_ack_max_ms"] = max(self.stale_ages_ms, default=None)
        result["freshness_reference"] = "api_ack_at_answer_completion"
        result["freshness_offered"] = total
        result["fresh_fraction"] = self.counts["fresh"] / total if total else None
        result["stale_fraction"] = self.counts["stale"] / total if total else None
        for deadline in DEADLINES_MS:
            result[f"fresh_on_time_{deadline}ms"] = (
                self.on_time[deadline] / total if total else None
            )
            result[f"fresh_on_time_upper_{deadline}ms"] = (
                (self.on_time[deadline] + self.uncertain_on_time[deadline]) / total
                if total
                else None
            )
        return result


def ideal_cache_rates(query_rate: float, update_rate: float, ttl_s: float) -> dict:
    """One Poisson name; instant fill/watch, infinite signature cache, unique versions."""
    if query_rate <= 0 or update_rate < 0 or ttl_s < 0:
        raise ValueError("positive queries, nonnegative updates and TTL required")
    fills = query_rate / (1 + query_rate * ttl_s)
    changed = -math.expm1(-update_rate * ttl_s)
    signs = fills * (
        update_rate / (query_rate + update_rate) + query_rate / (query_rate + update_rate) * changed
    )
    stale_time = ttl_s - changed / update_rate if update_rate else 0.0
    return {
        "response_fills_per_s": fills,
        "signs_per_s": signs,
        "stale_fraction": max(0.0, fills * stale_time),
    }


def interaction_contrasts(rows: list[dict]) -> list[dict]:
    """Paired algorithm gaps and their change from the same-TTL static control.

    Bounds propagate unknown freshness states, not sampling uncertainty. CPU
    differences can occur under a factorized cost model and are not proof of
    a freshness interaction.
    """
    matched_fields = (
        "repetition",
        "query_seed",
        "update_seed",
        "timeout_s",
        "max_inflight",
        "tcp_connections",
        "dynamic_warmup",
        "memory_limit",
        "query_rate_configured",
        "names",
        "policy",
        "arrival_mode",
        "popularity",
        "signature_cache_capacity",
        "cpu_limit",
        "sampling_interval_s",
        "replicas",
        "kubernetes_ttl",
        "warmup_s",
        "duration_s",
    )
    selected = [
        row
        for row in rows
        if row.get("study") == "algorithm_freshness_interaction"
        and row.get("verified_positive") is not None
    ]

    def key(row, algorithm=None, updates=None):
        return (
            *(row.get(field) for field in matched_fields),
            algorithm or row["algorithm"],
            row["update_rate_configured"] if updates is None else updates,
            row["response_cache_ttl"],
        )

    index = {key(row): row for row in selected}
    if len(index) != len(selected):
        raise ValueError("Ambiguous matched cells in the freshness comparison")

    def subtract(a, b, field):
        return (
            a[field] - b[field] if a.get(field) is not None and b.get(field) is not None else None
        )

    contrasts = []
    for row in selected:
        reference = index.get(key(row, "ED25519"))
        if row["algorithm"] == "ED25519" or reference is None:
            continue
        static = index.get(key(row, updates=0))
        static_reference = index.get(key(row, "ED25519", 0))
        result = {
            "cell_id": row["cell_id"],
            "reference_cell": reference["cell_id"],
            "repetition": row["repetition"],
            "algorithm": row["algorithm"],
            "response_cache_ttl": row["response_cache_ttl"],
            "update_rate": row["update_rate_configured"],
            "static_cell": static["cell_id"] if static else None,
        }
        for field in ("fresh_on_time_50ms", "stale_fraction", "signs_per_s", "cpu_cores"):
            gap = subtract(row, reference, field)
            baseline = (
                subtract(static, static_reference, field) if static and static_reference else None
            )
            result["algorithm_gap_" + field] = gap
            result["update_interaction_" + field] = (
                gap - baseline if gap is not None and baseline is not None else None
            )
        lower, upper = "fresh_on_time_50ms", "fresh_on_time_upper_50ms"
        if all(r.get(f) is not None for r in (row, reference) for f in (lower, upper)):
            lo, hi = row[lower] - reference[upper], row[upper] - reference[lower]
            result["fresh_50ms_gap_lower"], result["fresh_50ms_gap_upper"] = lo, hi
            if (
                static
                and static_reference
                and all(
                    r.get(f) is not None for r in (static, static_reference) for f in (lower, upper)
                )
            ):
                result["fresh_50ms_interaction_lower"] = lo - (
                    static[upper] - static_reference[lower]
                )
                result["fresh_50ms_interaction_upper"] = hi - (
                    static[lower] - static_reference[upper]
                )
        contrasts.append(result)
    return contrasts

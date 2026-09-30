import random

import pytest

from src.revision.cli import cell_schedule
from src.revision.freshness import VERIFIED, Freshness, ideal_cache_rates
from src.revision.workload import endpoint_address


def event(version, requested, acknowledged, error=None):
    return {
        "name": "x.",
        "index": 0,
        "version": version,
        "expected_a": endpoint_address(0, version),
        "requested_s": requested,
        "acknowledged_s": acknowledged,
        "error": error,
    }


def row(version, completed, latency=10, status="positive_unverified", verified=True):
    return {
        "query_id": 0,
        "fqdn": "x.",
        "status": status,
        "completed_s": completed,
        "answer_a": [endpoint_address(0, version)],
        "latency_ms": latency,
        "_verification": VERIFIED if verified else "invalid_positive",
    }


def oracle(events):
    return Freshness({"x.": {"address": endpoint_address(0, 0), "version": 0}}, events, True)


def test_update_during_query_counts_stale_at_delivery():
    f = oracle([event(1, 1, 2)])
    assert f.classify(row(0, 1.5))[0] == "unknown"
    assert f.classify(row(0, 2.1))[0] == "stale"
    assert f.classify(row(1, 2.1))[0] == "fresh"


def test_unknown_update_and_failure_do_not_improve_fresh_fraction():
    f = oracle([event(1, 1, 2), event(2, 5, 6)])
    for value in [
        row(1, 3),
        row(1, 3, latency=80),
        row(0, 3),
        row(2, 5.1),
        row(1, 3, verified=False),
        row(1, 3, status="client_rejected"),
    ]:
        f.observe(value)
    result = f.summary()
    assert result["freshness_offered"] == 6
    assert result["fresh_on_time_50ms"] == pytest.approx(1 / 6)
    assert result["fresh_on_time_upper_50ms"] == pytest.approx(2 / 6)
    assert result["freshness_failed_or_unverified"] == 2


def test_failed_update_stays_unknown_until_next_ack():
    f = oracle([event(1, 1, 2, "timeout"), event(2, 3, 4)])
    assert f.classify(row(0, 2.5))[0] == "unknown"
    assert f.classify(row(2, 4.1))[0] == "fresh"
    assert f.classify(row(1, 4.1))[0] == "stale"


def test_warmup_versions_are_available_at_measurement_start():
    f = oracle([event(1, -2, -1)])
    assert f.classify(row(0, 0.1)) == pytest.approx(("stale", 1100))
    assert f.classify(row(1, 0.1))[0] == "fresh"


def test_endpoint_versions_do_not_wrap_after_240_updates():
    assert endpoint_address(0, 0) == "10.250.0.1"
    addresses = {endpoint_address(4, v) for v in range(3000)}
    assert len(addresses) == 3000
    assert endpoint_address(5, 250) not in addresses


def test_randomized_blocks_are_reproducible_and_keep_every_cell():
    doc = {
        "cells": [{"id": str(i)} for i in range(9)],
        "repetitions": 3,
        "seed": 123,
        "cell_order": "randomized",
    }
    first = cell_schedule(doc)
    assert first == cell_schedule(doc)
    for rep in range(1, 4):
        assert {r["cell_id"] for r in first if r["repetition"] == rep} == {str(i) for i in range(9)}
    assert first[:9] != [{"repetition": 1, "cell_id": str(i)} for i in range(9)]


def test_ideal_null_matches_independent_event_simulation():
    q, u, ttl = 5.0, 0.5, 2.0
    queries, updates = random.Random(2), random.Random(90)
    now = expiry = 0.0
    next_update = updates.expovariate(u)
    version = cached = 0
    signatures = set()
    stale = signs = 0
    count = 100_000
    for _ in range(count):
        now += queries.expovariate(q)
        while next_update <= now:
            version += 1
            next_update += updates.expovariate(u)
        if now >= expiry:
            cached = version
            expiry = now + ttl
            if version not in signatures:
                signatures.add(version)
                signs += 1
        stale += cached < version
    expected = ideal_cache_rates(q, u, ttl)
    assert signs / now == pytest.approx(expected["signs_per_s"], abs=0.006)
    assert stale / count == pytest.approx(expected["stale_fraction"], abs=0.006)
    assert ideal_cache_rates(q, u, 0)["signs_per_s"] == pytest.approx(q * u / (q + u))
    assert ideal_cache_rates(q, 0, ttl)["stale_fraction"] == 0


def test_failed_service_queries_survive_missing_cpu_metrics(tmp_path):
    import gzip
    import json

    from src.revision.report import summarize_run

    config = {
        "algorithm_id": 0,
        "algorithm": "Baseline",
        "cell_id": "failed",
        "repetition": 1,
        "workload": "fixed_headless_endpoint_updates",
        "transport_policy": "fresh_tcp",
    }
    load = {"duration_s": 1, "offered": 1, "counts": {"timeout": 1}}
    query = {
        "query_id": 0,
        "fqdn": "x.",
        "planned_s": 0,
        "admitted_s": 0.001,
        "sent_s": 0.201,
        "completed_s": 1,
        "status": "timeout",
        "answer_a": [],
        "attempts": [],
        "latency_ms": 1000,
        "wire_latency_ms": None,
        "dispatch_lag_ms": None,
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    (tmp_path / "load.json").write_text(json.dumps(load))
    with gzip.open(tmp_path / "queries.jsonl.gz", "wt") as file:
        file.write(json.dumps(query) + "\n")
    with gzip.open(tmp_path / "responses.bin.gz", "wb"):
        pass
    result = summarize_run(tmp_path)
    assert result["offered"] == result["failure_or_unknown"] == 1
    assert result["fresh_on_time_50ms"] == 0
    assert result["cpu_cores"] is None
    assert result["cpu_metrics_valid"] is False
    assert result["admission_p99_ms"] == pytest.approx(1)
    assert result["send_wait_p99_ms"] == pytest.approx(200)


@pytest.mark.parametrize("q,u,ttl", [(5, 0.5, 2), (3, 2, 5), (10, 0.01, 1), (100, 10, 0)])
def test_ideal_linear_cost_has_no_unique_interior_ttl_optimum(q, u, ttl):
    rates = ideal_cache_rates(q, u, ttl)
    zero_ttl_signs = q * u / (q + u)
    assert rates["signs_per_s"] == pytest.approx(zero_ttl_signs * (1 - rates["stale_fraction"]))


def test_boundary_only_sampling_must_be_declared(tmp_path):
    import json

    from src.revision.metrics import validate_window

    rows = [
        {
            "uid": "pod",
            "phase": phase,
            "monotonic_s": at,
            "series": {
                "process_cpu_seconds_total": cpu,
                "process_resident_memory_bytes": 1024,
                "process_start_time_seconds": 10,
            },
        }
        for phase, at, cpu in [("measurement_start", 100, 1), ("measurement_end", 130, 4)]
    ]
    path = tmp_path / "metrics.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)))
    assert validate_window(path, 30, 1, 0)["pod"]["cpu_seconds"] == 3
    with pytest.raises(RuntimeError, match="Only 0 valid periodic"):
        validate_window(path, 30, 1, 1)


def test_paired_contrast_separates_cpu_scaling_from_freshness_interaction():
    from src.revision.freshness import interaction_contrasts

    rows = []
    for algorithm, multiplier in [("ED25519", 1), ("ML-DSA-44", 2)]:
        for updates in (0, 4):
            rows.append(
                {
                    "study": "algorithm_freshness_interaction",
                    "verified_positive": 100,
                    "cell_id": f"{algorithm}-{updates}",
                    "algorithm": algorithm,
                    "repetition": 1,
                    "update_rate_configured": updates,
                    "response_cache_ttl": 1,
                    "fresh_on_time_50ms": 1 if not updates else 0.8,
                    "fresh_on_time_upper_50ms": 1 if not updates else 0.9,
                    "stale_fraction": 0 if not updates else 0.1,
                    "signs_per_s": updates,
                    "cpu_cores": multiplier * (0.1 if not updates else 0.3),
                }
            )
    result = next(r for r in interaction_contrasts(rows) if r["update_rate"] == 4)
    assert result["update_interaction_fresh_on_time_50ms"] == 0
    assert result["update_interaction_signs_per_s"] == 0
    assert result["update_interaction_cpu_cores"] == pytest.approx(0.2)
    assert result["fresh_50ms_interaction_lower"] == pytest.approx(-0.1)
    assert result["fresh_50ms_interaction_upper"] == pytest.approx(0.1)
    rows[-1]["cpu_cores"] = None
    assert interaction_contrasts(rows)[-1]["update_interaction_cpu_cores"] is None


def test_different_query_seeds_are_not_a_paired_contrast():
    from src.revision.freshness import interaction_contrasts

    rows = [
        {
            "study": "algorithm_freshness_interaction",
            "verified_positive": 100,
            "cell_id": algorithm,
            "algorithm": algorithm,
            "repetition": 1,
            "update_rate_configured": 4,
            "response_cache_ttl": 1,
            "query_seed": seed,
        }
        for algorithm, seed in [("ED25519", 1), ("ML-DSA-44", 2)]
    ]
    assert interaction_contrasts(rows) == []

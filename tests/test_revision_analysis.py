import json

import pytest

from src.revision.analysis import (
    offered_integral,
    popularity_weights,
    predicted_signs,
    stationary_signs_per_s,
)


def test_burst_profile_preserves_the_configured_mean():
    assert offered_integral("burst_2_of_20", 100, 30, 0, 30) == pytest.approx(3000)
    high = offered_integral("burst_2_of_20", 100, 30, 0, 2)
    low = offered_integral("burst_2_of_20", 100, 30, 2, 4)
    assert high / low == pytest.approx(10)


def test_stationary_rate_approaches_update_rate_and_is_bounded():
    uniform = popularity_weights("uniform", 32)
    assert stationary_signs_per_s(100, 1, uniform) == pytest.approx(100 / 101)
    assert stationary_signs_per_s(100, 1e6, uniform) < 100
    zipf = popularity_weights("zipf_1_1", 32)
    assert sum(zipf) == pytest.approx(1)
    assert stationary_signs_per_s(100, 1, zipf) < stationary_signs_per_s(100, 1, uniform)


def test_prediction_uses_logged_updates(tmp_path):
    config = {
        "cell_id": "x",
        "repetition": 1,
        "algorithm_id": 18,
        "signature_cache_capacity": 1024,
        "names": 1,
        "popularity": "uniform",
        "arrival_mode": "poisson",
        "query_rate": 10.0,
        "replicas": 1,
    }
    (tmp_path / "config.json").write_text(json.dumps(config))
    (tmp_path / "load.json").write_text(json.dumps({"duration_s": 30}))
    summary = {"dns_transactions": 2, "offered": 1, "signs_per_s": 1 / 30, "cpu_window_min_s": 30}
    (tmp_path / "summary.json").write_text(json.dumps(summary))
    events = [
        {"name": "a.", "index": 0, "requested_s": 1.0, "acknowledged_s": 1.0, "error": None},
        {"name": "a.", "index": 0, "requested_s": 1.1, "acknowledged_s": 1.1, "error": None},
    ]
    (tmp_path / "updates.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    result = predicted_signs(tmp_path)
    # The first version lives 0.1 s (one expected query), the second until the end.
    first = 1 - 2.718281828459045 ** (-1.0)
    second = 1 - 2.718281828459045 ** (-289.0)
    assert result["predicted"] == pytest.approx(first + second)
    assert result["measured"] == 1

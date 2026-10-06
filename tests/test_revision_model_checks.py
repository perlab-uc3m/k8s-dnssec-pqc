import pytest

from src.revision.model_checks import offered_cache_model


def sample(**changes):
    return {
        "names": 32,
        "replicas": 1,
        "offered": 3000,
        "duration_s": 30,
        "updates_acknowledged": 240,
        "response_cache_ttl": 0,
        **changes,
    }


def test_demand_does_not_fall_when_queries_fail():
    full = offered_cache_model(sample(dns_transactions=6000, positive_unverified=3000))
    failed = offered_cache_model(sample(dns_transactions=100, positive_unverified=50))
    assert full == failed
    assert full["signs_per_s"] == pytest.approx(100 * 8 / 108)


def test_replica_prediction_uses_both_fallback_exchanges():
    fallback = offered_cache_model(sample(replicas=2), lookups_per_query=2)
    direct = offered_cache_model(sample(replicas=2), lookups_per_query=1)
    assert fallback["signs_per_s"] == pytest.approx(2 * 75 * 8 / 83)
    assert direct["signs_per_s"] == pytest.approx(2 * 50 * 8 / 58)


def test_stationary_check_rejects_bursts_and_skew():
    for change in [{"arrival_mode": "burst_2_of_20"}, {"popularity": "zipf_1_1"}]:
        with pytest.raises(ValueError, match="uniform Poisson"):
            offered_cache_model(sample(**change))

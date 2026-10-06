import pytest

from src.revision.dynamics import phase_means


def query(sent, wire, planned=0):
    return {
        "status": "positive_unverified",
        "sent_s": sent,
        "wire_latency_ms": wire,
        "planned_s": planned,
    }


def test_rollout_phase_uses_first_write_and_complete_cycles():
    # The final cycle at 9 s is incomplete. A dispatched query crosses a bin
    # boundary relative to its planned time and must use the actual first write.
    result = phase_means(
        [query(1.2, 10), query(1.6, 30, 1.2), query(9.2, 999)], [1, 9], 10, [0, 0.5, 1, 8]
    )
    assert result["cycles"] == 1
    assert result["counts"] == [1, 1, 0]
    assert result["mean_exchange_ms"] == [10, 30, None]


def test_rollout_phase_boundary_belongs_to_following_cycle():
    result = phase_means([query(1, 10), query(9, 30), query(17, 999)], [1, 9], 17, [0, 4, 8])
    assert result["cycles"] == 2
    assert result["counts"] == [2, 0]
    assert result["mean_exchange_ms"] == [20, None]


def test_rollout_phase_refuses_to_hide_failed_queries():
    with pytest.raises(ValueError, match="complete positive"):
        phase_means([query(1.2, 10) | {"status": "timeout"}], [1], 9, [0, 8])

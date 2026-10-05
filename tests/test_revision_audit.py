import gzip
import json
import struct

import pytest

from src.revision.metrics import measurement_window, window_elapsed
from src.revision.report import _metric_delta, _visibility, summarize_run


def test_legacy_clock_reset_uses_periodic_snapshot():
    rows = [
        {"phase": "measurement_start", "t_s": 2.0, "series": {"process_cpu_seconds_total": 10}},
        {"phase": "measurement", "t_s": 0.01, "series": {"process_cpu_seconds_total": 10.1}},
        {"phase": "measurement_end", "t_s": 30.01, "series": {"process_cpu_seconds_total": 25.1}},
    ]
    first, last, source = measurement_window(rows)
    assert source == "legacy_first_periodic_to_end"
    assert window_elapsed(first, last) == pytest.approx(30)
    assert _metric_delta(rows, "process_cpu_seconds_total") == pytest.approx((15, 30))
    assert _metric_delta(rows, "missing_counter") is None


def test_absolute_clock_overrides_relative_timestamps():
    rows = [
        {"phase": "measurement_start", "monotonic_s": 100, "t_s": 2},
        {"phase": "measurement_end", "monotonic_s": 131, "t_s": 30},
    ]
    first, last, source = measurement_window(rows)
    assert source == "absolute_monotonic_boundaries"
    assert window_elapsed(first, last) == 31


def test_signed_run_cannot_silently_become_unsigned(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"algorithm_id": 18}))
    (tmp_path / "load.json").write_text("{}")
    for name in ("queries.jsonl.gz", "responses.bin.gz"):
        with gzip.open(tmp_path / name, "wb"):
            pass
    with pytest.raises(ValueError, match="Missing signature verification"):
        summarize_run(tmp_path)


def test_frame_length_and_attempt_match_query(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"algorithm_id": 0}))
    (tmp_path / "load.json").write_text("{}")
    row = {
        "query_id": 0,
        "fqdn": "x.",
        "status": "positive_unverified",
        "answer_a": ["10.0.0.1"],
        "attempts": [{"transport": "udp", "wire_bytes": 3}],
    }
    with gzip.open(tmp_path / "queries.jsonl.gz", "wt") as out:
        out.write(json.dumps(row) + "\n")
    with gzip.open(tmp_path / "responses.bin.gz", "wb") as out:
        out.write(struct.pack(">QBI", 0, 1, 3) + b"abc")
    with pytest.raises(ValueError, match="frame accounting"):
        summarize_run(tmp_path)


def test_freshness_respects_versions_final_transaction_and_next_request(tmp_path):
    (tmp_path / "load.json").write_text(json.dumps({"duration_s": 10}))
    events = [
        {
            "name": "x.",
            "index": 0,
            "version": 1,
            "expected_a": "10.250.0.2",
            "requested_s": 1,
            "acknowledged_s": 2,
        },
        {
            "name": "x.",
            "index": 0,
            "version": 2,
            "expected_a": "10.250.0.3",
            "requested_s": 5,
            "acknowledged_s": 6,
        },
    ]
    (tmp_path / "updates.jsonl").write_text("\n".join(map(json.dumps, events)))

    def observation(sent, done, address):
        return {
            "status": "positive_unverified",
            "sent_s": 0.5,
            "completed_s": done,
            "attempts": [{"sent_s": 0.5}, {"sent_s": sent}],
            "answer_a": [address],
            "_verification": "private_zone_signature_verified",
        }

    rows = [
        observation(3.5, 4, "10.250.0.2"),
        observation(2.4, 2.5, "10.250.0.2"),
        observation(2.1, 2.2, "10.250.0.1"),
        observation(4.9, 5.2, "10.250.0.3"),
        observation(6.1, 6.2, "10.250.0.3"),
    ]
    result = _visibility(tmp_path, {"x.": rows}, True)
    assert result["post_ack_positive_observations"] == 4
    assert result["stale_post_ack_answers"] == 1
    assert result["updates_censored"] == 0
    assert result["visibility_upper_p50_ms"] == pytest.approx(350)


def test_changed_build_input_is_rejected(tmp_path, monkeypatch):
    from src.revision import cli

    monkeypatch.setattr(cli, "ROOT", tmp_path)
    build = tmp_path / "build"
    build.mkdir()
    source = tmp_path / "signer.patch"
    source.write_text("first version")
    (build / "coredns-pqc").write_bytes(b"compiled source")
    record = {
        "inputs": {"signer.patch": cli.artifact_hash(source)},
        "binary_sha256": cli.artifact_hash(build / "coredns-pqc"),
    }
    (build / "source-manifest.json").write_text(json.dumps(record))
    assert cli.validated_build_manifest() == record
    source.write_text("changed version")
    with pytest.raises(ValueError, match="Build input changed"):
        cli.validated_build_manifest()


def test_resume_cannot_mix_builds_or_adopt_archived_runs(tmp_path):
    from src.revision.cli import freeze_acquisition

    record = {"build": {"signature_ownership_fix": True}, "acquisition": {"cli.py": "abc"}}
    freeze_acquisition(tmp_path, record)
    freeze_acquisition(tmp_path, record)
    with pytest.raises(ValueError, match="source changed"):
        freeze_acquisition(tmp_path, {"build": {"signature_ownership_fix": False}})
    (tmp_path / "acquisition_manifest.json").unlink()
    complete = tmp_path / "runs/cell/rep-01/attempt-01/COMPLETE"
    complete.parent.mkdir(parents=True)
    complete.touch()
    with pytest.raises(ValueError, match="Archived campaign"):
        freeze_acquisition(tmp_path, record)


def test_lazy_coalescence_counter_preserves_first_events_and_zero():
    prefix = "coredns_dnssec_pqc_singleflight_coalesced_total"
    base = {
        "process_start_time_seconds": 1,
        'coredns_dnssec_pqc_singleflight_execs_total{server="dns://:53"}': 100,
    }
    rows = [
        {"phase": "measurement_start", "monotonic_s": 100, "series": dict(base)},
        {"phase": "measurement_end", "monotonic_s": 130, "series": dict(base)},
    ]
    assert _metric_delta(rows, prefix, lazy_coalescence=True) == (0, 30)
    rows[-1]["series"][prefix + '{server="dns://:53"}'] = 2
    assert _metric_delta(rows, prefix, lazy_coalescence=True) == (2, 30)
    assert _metric_delta(rows, prefix) is None
    rows[-1]["series"]["process_start_time_seconds"] = 2
    assert _metric_delta(rows, prefix, lazy_coalescence=True) is None
    rows[-1]["series"] = {}
    assert _metric_delta(rows, prefix, lazy_coalescence=True) is None


def test_paper_export_rejects_incomplete_or_mixed_evidence(tmp_path):
    from src.revision.paper import validate_paper_campaign

    (tmp_path / "campaign.yaml").write_text("repetitions: 2\ncells: [{id: one}]\n")
    for rep in (1, 2):
        run = tmp_path / f"runs/one/rep-{rep:02d}/attempt-01"
        run.mkdir(parents=True)
        (run / "config.json").write_text(json.dumps({"cell_id": "one", "repetition": rep}))
        (run / "host.json").write_text(
            json.dumps({"cpu_model": "host A", "build": {"image_id": "a"}})
        )
        (run / "COMPLETE").touch()
    validate_paper_campaign(tmp_path)
    (run / "host.json").write_text(json.dumps({"cpu_model": "host B", "build": {"image_id": "b"}}))
    with pytest.raises(ValueError, match="different measured builds or hosts"):
        validate_paper_campaign(tmp_path)
    (run / "COMPLETE").unlink()
    with pytest.raises(ValueError, match="every declared cell"):
        validate_paper_campaign(tmp_path)

import asyncio
import json
import socket

import pytest
import yaml

from src.revision.analysis import answered_fraction, predicted_cache_rates
from src.revision.cli import config
from src.revision.final import check_smoke
from src.revision.netem import netem_args
from src.revision.queries import _udp


def test_netem_arguments():
    assert netem_args(10) == [
        "tc",
        "qdisc",
        "replace",
        "dev",
        "eth0",
        "root",
        "netem",
        "delay",
        "10ms",
        "limit",
        "100000",
    ]
    assert netem_args(0, 1)[7:9] == ["loss", "1%"]
    for bad in ((0, 0), (-1, 0), (0, 100)):
        with pytest.raises(ValueError):
            netem_args(*bad)


def _config(tmp_path, doc):
    path = tmp_path / "c.yaml"
    path.write_text(yaml.safe_dump(doc))
    return config(path)


def test_campaign_validation_rejects_bad_options(tmp_path):
    doc = yaml.safe_load(open("config/revision-final.yaml"))
    assert _config(tmp_path, doc)
    for field, value in (
        ("pod_egress_delay_ms", -1),
        ("pod_egress_loss_pct", 100),
        ("udp_retry_s", 0),
        ("update_batch", 33),
    ):
        broken = json.loads(json.dumps(doc))
        broken["cells"][0][field] = value
        with pytest.raises(ValueError, match=field):
            _config(tmp_path, broken)


def test_final_campaign_covers_every_block():
    doc = yaml.safe_load(open("config/revision-final.yaml"))
    ids = [cell["id"] for cell in doc["cells"]]
    assert len(ids) == 56 and len(set(ids)) == 56
    algorithms = {cell["algorithm"] for cell in doc["cells"]}
    assert len(algorithms) == 13  # twelve signers and the unsigned control
    assert doc["repetitions"] == 5 and doc["require_ownership_fix"]
    for cell in doc["cells"]:
        merged = {**doc, **cell}
        assert merged["record_ttl"] >= merged["response_cache_ttl"]
        assert 1 <= merged["names"] <= 250
        assert merged["update_batch"] <= merged["names"]
    from src.revision.final import CAPACITY, COALESCENCE, DELAY, LOAD, LOSS, MODEL, ROLLOUT, SURVEY

    used = set(SURVEY) | {c for c, _ in CAPACITY + MODEL + COALESCENCE}
    used |= {c for _, cells in DELAY for c in cells}
    used |= {c for pair in LOAD + LOSS + ROLLOUT for c in pair[:2]}
    assert used <= set(ids), used - set(ids)
    smoke = yaml.safe_load(open("config/revision-final-smoke.yaml"))
    smoke_ids = {c["id"] for c in smoke["cells"]}
    assert smoke_ids <= set(ids)
    assert {c["algorithm"] for c in smoke["cells"]} | {
        "Baseline",
        "ED25519",
        "Falcon-512",
    } == algorithms


def test_response_cache_model_reduces_to_equation_one():
    summary = {
        "names": 32,
        "replicas": 1,
        "duration_s": 30,
        "updates_acknowledged": 30,
        "popularity": "uniform",
        "dns_transactions": 2,
        "offered": 1,
        "response_cache_ttl": 0,
        "query_rate_configured": 100,
    }
    assert predicted_cache_rates(summary)["signs_per_s"] == pytest.approx(100 / 101)
    assert predicted_cache_rates(summary)["stale_fraction"] == 0
    cached = predicted_cache_rates({**summary, "response_cache_ttl": 5})
    assert cached["signs_per_s"] < 100 / 101 and 0 < cached["stale_fraction"] < 1
    static = predicted_cache_rates({**summary, "updates_acknowledged": 0, "names": 1})
    assert static["signs_per_s"] == 0


def _write_run(path, rows, verified):
    import gzip

    path.mkdir(parents=True)
    with gzip.open(path / "queries.jsonl.gz", "wt") as out:
        for row in rows:
            out.write(json.dumps(row) + "\n")
    with (path / "verification.jsonl").open("w") as out:
        for row in rows:
            state = "private_zone_signature_verified" if row["query_id"] in verified else "invalid"
            out.write(json.dumps({"query_id": row["query_id"], "verification": state}) + "\n")


def test_answered_fraction_requires_verification_and_deadline(tmp_path):
    rows = [
        {"query_id": 0, "status": "positive_unverified", "latency_ms": 3.0},
        {"query_id": 1, "status": "positive_unverified", "latency_ms": 1500.0},
        {"query_id": 2, "status": "positive_unverified", "latency_ms": 2.0},
        {"query_id": 3, "status": "timeout", "latency_ms": 5000.0},
    ]
    _write_run(tmp_path / "run", rows, verified={0, 1})
    assert answered_fraction(tmp_path / "run", 1000) == 0.25


def test_udp_resend_recovers_a_lost_reply():
    async def scenario():
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.bind(("127.0.0.1", 0))
        server.setblocking(False)
        port = server.getsockname()[1]
        loop = asyncio.get_running_loop()
        seen = []

        async def serve():
            while True:
                data, addr = await loop.sock_recvfrom(server, 512)
                seen.append(data)
                if len(seen) >= 2:  # drop the first copy, answer the resend
                    await loop.sock_sendto(server, b"reply", addr)
                    return

        task = asyncio.create_task(serve())
        sends = []
        reply, _ = await asyncio.wait_for(
            _udp("127.0.0.1", port, b"query", sends.append, retry_s=0.05), 2
        )
        await task
        server.close()
        return reply, len(sends), len(seen)

    reply, sends, seen = asyncio.run(scenario())
    assert reply == b"reply" and sends == 2 and seen == 2


def test_rollout_schedule_keeps_the_mean_rate(tmp_path, monkeypatch):
    import random

    from src.revision.workload import HeadlessWorkload

    fixture = HeadlessWorkload.__new__(HeadlessWorkload)
    fixture.namespace, fixture.domain, fixture.count = "bench", "cluster.local", 32
    fixture.names = [f"pqc-{i:03d}" for i in range(32)]
    fixture.versions = [0] * 32
    fixture.rng = random.Random(1)
    monkeypatch.setattr(fixture, "_update", lambda index, version: "1")

    async def run():
        import time

        return await fixture.changes(tmp_path, time.monotonic() - 100, 30, 1.0, seed=7, batch=8)

    result = asyncio.run(run())
    events = [json.loads(line) for line in (tmp_path / "updates.jsonl").read_text().splitlines()]
    planned = sorted({e["planned_s"] for e in events})
    assert result["batch"] == 8 and len(events) == 8 * len(planned)
    assert all(b - a == pytest.approx(8) for a, b in zip(planned, planned[1:], strict=False))
    for when in planned:
        names = [e["name"] for e in events if e["planned_s"] == when]
        assert len(set(names)) == 8


def test_smoke_check_flags_missing_netem_and_unverified_answers(tmp_path):
    campaign = tmp_path / "smoke"
    campaign.mkdir()
    (campaign / "campaign.yaml").write_text(
        "cells:\n  - id: mldsa44-delay10\n  - id: sphincs128s-u4\n"
    )
    base = {
        "algorithm": "ML-DSA-44",
        "positive_unverified": 10,
        "verified_positive": 10,
        "pod_egress_delay_ms": 0,
        "pod_egress_loss_pct": 0,
        "wire_p50_ms": 1.0,
        "cpu_metrics_valid": True,
    }
    for cell, extra in (
        ("mldsa44-delay10", {"pod_egress_delay_ms": 10, "wire_p50_ms": 31.0}),
        ("sphincs128s-u4", {"verified_positive": 9}),
    ):
        run = campaign / "runs" / cell / "rep-01" / "attempt-01"
        run.mkdir(parents=True)
        (run / "COMPLETE").write_text("x")
        (run / "summary.json").write_text(
            json.dumps({**base, **extra, "cell_id": cell, "run": str(run)})
        )
    problems = check_smoke(campaign)
    assert any("netem record missing" in p for p in problems)
    assert any("failed verification" in p for p in problems)
    run = campaign / "runs" / "mldsa44-delay10" / "rep-01" / "attempt-01"
    (run / "netem.json").write_text('[{"qdisc": "qdisc netem 8001: root delay 10ms"}]')
    assert not any("netem" in p for p in check_smoke(campaign))


def test_smoke_check_requires_cpu_window_except_near_sphincs_capacity(tmp_path):
    campaign = tmp_path / "smoke"
    campaign.mkdir()
    (campaign / "campaign.yaml").write_text(
        "cells:\n  - id: mldsa44-q400\n  - id: sphincs128s-u8-2core\n"
    )
    for cell, algorithm in (
        ("mldsa44-q400", "ML-DSA-44"),
        ("sphincs128s-u8-2core", "SPHINCS+-SHA2-128s-simple"),
    ):
        run = campaign / "runs" / cell / "rep-01" / "attempt-01"
        run.mkdir(parents=True)
        (run / "COMPLETE").write_text("x")
        row = {
            "cell_id": cell,
            "run": str(run),
            "algorithm": algorithm,
            "positive_unverified": 10,
            "verified_positive": 10,
            "cpu_metrics_valid": False,
        }
        (run / "summary.json").write_text(json.dumps(row))
    problems = check_smoke(campaign)
    assert any(p.startswith("mldsa44-q400: missing or invalid CPU window") for p in problems)
    assert not any(p.startswith("sphincs128s-u8-2core") for p in problems)


def test_apply_netem_enters_the_pod_namespace(monkeypatch):
    from src.revision import netem

    calls = []

    def fake(cmd, text, timeout):
        calls.append(cmd)
        if cmd[3:5] == ["crictl", "ps"]:
            return "abc123\n"
        if cmd[3:5] == ["crictl", "inspect"]:
            return json.dumps({"info": {"pid": 4242}})
        if "show" in cmd:
            return "qdisc netem 8001: root refcnt 2 limit 100000 delay 10ms loss 1%\n"
        return ""

    monkeypatch.setattr(netem.subprocess, "check_output", fake)
    records = netem.apply_netem([{"name": "coredns-pqc-x", "node": "kind-node"}], 10, 1)
    assert records[0]["pid"] == 4242
    install = next(c for c in calls if "replace" in c)
    assert install[:8] == ["docker", "exec", "kind-node", "nsenter", "-t", "4242", "-n", "tc"]
    assert "loss" in install and "delay" in install


def test_energy_sums_interval_wraps_and_rejects_missing_samples():
    from src.revision.resources import package_power_from_samples

    rows = [
        dict(monotonic_s=i, rapl_energy_uj=v, rapl_max_energy_uj=1000000)
        for i, v in enumerate((900000, 500000, 100000, 700000))
    ]
    assert package_power_from_samples(rows) == pytest.approx(0.6)
    del rows[1]["rapl_energy_uj"]
    assert package_power_from_samples(rows) is None


def test_final_controls_match_budgets_and_isolate_singleflight():
    doc = yaml.safe_load(open("config/revision-final.yaml"))
    cells = {c["id"]: {**doc, **c} for c in doc["cells"]}
    a = cells["sphincs128s-u4-2core"]
    b = cells["sphincs128s-u4-2pods-1core"]
    assert a["update_rate"] == b["update_rate"] == 4
    assert float(a["cpu_limit"]) == b["replicas"] * float(b["cpu_limit"])
    for c in cells.values():
        if "nocache-1name" in c["id"]:
            assert c["transport_policy"] == "fresh_tcp"
        if "n250" in c["id"]:
            assert c["update_rate"] == 50 and c["measurement_s"] == 60
            assert c["sig_cache_cap"] >= 1024


def test_both_exports_accept_five_repetitions_and_missing_stress_cpu(tmp_path, monkeypatch):
    """Exercise both real exporters; fabricated values test mechanics, not performance."""
    from src.revision import analysis, final, paper

    doc = yaml.safe_load(open("config/revision-final.yaml"))
    (tmp_path / "campaign.yaml").write_text(yaml.safe_dump(doc))
    rows = []
    for cell in doc["cells"]:
        cfg = {**doc, **cell}
        for rep in range(1, 6):
            run = tmp_path / "runs" / cell["id"] / f"rep-{rep:02}" / "attempt-01"
            run.mkdir(parents=True)
            (run / "COMPLETE").touch()
            (run / "config.json").write_text(json.dumps({"cell_id": cell["id"], "repetition": rep}))
            (run / "host.json").write_text(
                json.dumps({"build": {"image_id": "test"}, "cpu_model": "test"})
            )
            row = dict(
                run=str(run),
                cell_id=cell["id"],
                repetition=rep,
                algorithm=cell["algorithm"],
                names=cfg["names"],
                replicas=cfg.get("replicas", 1),
                cpu_limit=cfg["cpu_limit"],
                duration_s=cfg["measurement_s"],
                pod_egress_delay_ms=cfg["pod_egress_delay_ms"],
                offered=100,
                verified_positive=100 if cell["algorithm"] != "Baseline" else None,
                positive_unverified=100,
                failure_or_unknown=0,
                freshness_stale=0,
                freshness_unknown=0,
                updates_acknowledged=10,
                throttled_period_fraction=0,
                host_available_min_bytes=10 * 2**30,
                host_swap_out_pages=0,
                cpu_metrics_valid=True,
                cpu_cores=0.2,
                client_cpu_cores=0.1,
                p99_ms=5.0,
                wire_p99_ms=3.0,
                wire_p50_ms=1.0 + cfg["pod_egress_delay_ms"],
                dispatch_p99_ms=2.0,
                signs_per_s=1.0,
                sign_wall_mean_ms=0.1,
                fallback_fraction=0.0,
                final_wire_p50_bytes=838,
                udp_wire_p50_bytes=838,
                singleflight_coalesced=0,
                signature_cache_hit_fraction=0.99,
                signature_cache_evictions=0,
                host_package_power_w=None,
                fresh_on_time_1000ms=1.0,
                stale_fraction=0.0,
                dns_transactions=100,
                coalescence_observed=0.0,
                offered_qps=100 / cfg["measurement_s"],
                retransmitted_queries=0,
            )
            if cell["id"] in {"sphincs128s-u12", "sphincs128s-rollout"}:
                row.update(
                    cpu_cores=None,
                    cpu_metrics_valid=False,
                    signs_per_s=None,
                    signature_cache_evictions=None,
                    throttled_period_fraction=None,
                )
            rows.append(row)
    monkeypatch.setattr(paper, "report", lambda _: rows)
    monkeypatch.setattr(final, "report", lambda _: rows)
    monkeypatch.setattr(
        analysis,
        "predicted_signs",
        lambda run: {
            "updates": 10,
            "predicted": 9.0,
            "measured": None if run.parts[-3] == "sphincs128s-rollout" else 9,
        },
    )
    monkeypatch.setattr(
        analysis, "algorithm_sizes", lambda _: {"public_key": 32, "signature_median": 64}
    )
    monkeypatch.setattr(
        analysis,
        "predicted_cache_rates",
        lambda _: {"signs_per_s": 1, "signs_per_pod": 1, "stale_fraction": 0},
    )
    monkeypatch.setattr(analysis, "answered_fraction", lambda *_: 1.0)
    monkeypatch.setattr(analysis, "mean_inflight", lambda _: None)
    monkeypatch.setattr(
        analysis, "latency_quantiles", lambda _: {"q0.5": 1, "q0.99": 3, "q0.999": 5, "max": 6}
    )
    monkeypatch.setattr(analysis, "cpu_per_second", lambda *_: [(40, 0.01), (400, 0.1)])
    monkeypatch.setattr(
        analysis,
        "burst_phase_p99",
        lambda _: {
            phase: {"dispatch_p99_ms": 2, "exchange_p99_ms": 3} for phase in ("low", "high")
        },
    )
    import numpy as np

    monkeypatch.setattr(analysis, "exchange_times", lambda *_: np.array([1.0, 2.0, 3.0]))
    reference = paper.export_paper(tmp_path, tmp_path / "export/paper")
    assert reference["offered"] == 12 * 5 * 100
    for name in ("table_signing.tex", "table_results.tex"):
        text = (tmp_path / "export/paper" / name).read_text()
        assert "three" not in text and "84" not in text and "85" not in text
    expanded = final.export_final(tmp_path, tmp_path / "export/final")
    assert expanded["runs"] == 280
    assert expanded["model"]["sphincs128s-rollout"]["measured"] is None
    assert expanded["capacity"]["sphincs128s-u12"]["cpu_share"] is None
    assert "energy" not in expanded
    assert len(list((tmp_path / "export/final").glob("*.pdf"))) == 3


def test_exports_reject_failed_signature_verification():
    from src.revision.paper import validate_verified_answers

    row = {
        "algorithm": "ML-DSA-44",
        "cell_id": "test",
        "positive_unverified": 10,
        "verified_positive": 9,
    }
    with pytest.raises(ValueError, match="invalid signed answers"):
        validate_verified_answers([row])
    validate_verified_answers([{**row, "verified_positive": 10}])
    validate_verified_answers([{**row, "algorithm": "Baseline", "verified_positive": None}])

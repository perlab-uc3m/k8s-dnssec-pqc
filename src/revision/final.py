"""Export of the final revision campaign (config/revision-final.yaml).

Every quantity uses all complete repetitions. Predictions are fixed in advance:
Eq. 1 and its response-cache form give signing rates and stale shares, Eq. 2 gives the
shared fraction of concurrent misses, signing demand is the predicted signing rate times
the measured time per signature, and the slope of exchange time against added delay
counts the client round trips that reach the pod.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import numpy as np
import yaml

from . import analysis
from .paper import validate_paper_campaign, validate_verified_answers
from .report import report

LABELS = {
    "Baseline": "Unsigned",
    "ED25519": "Ed25519",
    "ECDSAP256SHA256": "ECDSA P-256",
    "RSASHA256": "RSA-2048",
    "Falcon-512": "Falcon-512",
    "Falcon-1024": "Falcon-1024",
    "ML-DSA-44": "ML-DSA-44",
    "ML-DSA-65": "ML-DSA-65",
    "ML-DSA-87": "ML-DSA-87",
    "MAYO-1": "MAYO-1",
    "MAYO-3": "MAYO-3",
    "SNOVA_24_5_4": "SNOVA (24,5,4)",
    "SPHINCS+-SHA2-128s-simple": "SPHINCS+-128s",
}
SURVEY = [
    "unsigned-reference",
    "ed25519-reference",
    "ecdsap256-reference",
    "rsasha256-reference",
    "falcon512-reference",
    "mayo1-reference",
    "mayo3-reference",
    "snova-reference",
    "falcon1024-reference",
    "mldsa44-reference",
    "mldsa65-reference",
    "mldsa87-reference",
    "sphincs128s-u1",
]
CAPACITY = [
    ("sphincs128s-u1", "SPHINCS+, 1 update/s"),
    ("sphincs128s-u4", "SPHINCS+, 4 updates/s"),
    ("sphincs128s-u8", "SPHINCS+, 8 updates/s"),
    ("sphincs128s-u12", "SPHINCS+, 12 updates/s"),
    ("sphincs128s-u8-ttl1", "SPHINCS+, 8/s, TTL 1 s"),
    ("sphincs128s-u8-ttl5", "SPHINCS+, 8/s, TTL 5 s"),
    ("sphincs128s-u12-ttl5", "SPHINCS+, 12/s, TTL 5 s"),
    ("sphincs128s-u4-2core", "SPHINCS+, 4/s, 1 pod $\\times$ 2 cores"),
    ("sphincs128s-u8-2core", "SPHINCS+, 8/s, 1 pod $\\times$ 2 cores"),
    ("sphincs128s-u12-2core", "SPHINCS+, 12/s, 1 pod $\\times$ 2 cores"),
    ("sphincs128s-u4-2pods-1core", "SPHINCS+, 4/s, 2 pods $\\times$ 1 core"),
    ("sphincs128s-u4-2pods-0.5core", "SPHINCS+, 4/s, 2 pods $\\times$ 0.5 core"),
    ("mldsa44-u8", "ML-DSA-44, 8 updates/s"),
    ("mldsa44-u8-ttl1", "ML-DSA-44, 8/s, TTL 1 s"),
    ("mldsa44-u8-ttl5", "ML-DSA-44, 8/s, TTL 5 s"),
]
MODEL = [
    ("mldsa44-reference", "Reference, 1 update/s"),
    ("mldsa44-fast-updates", "10 updates/s"),
    ("mldsa44-u50", "50 updates/s"),
    ("mldsa44-zipf", "Zipf 1.1"),
    ("mldsa44-burst", "Bursts"),
    ("mldsa44-two-replicas", "Two replicas"),
    ("mldsa44-rollout", "Rollouts, 8 names every 8 s"),
    ("sphincs128s-rollout", "Rollouts, SPHINCS+"),
    ("mldsa44-n250-u50-cap9984", "250 names, 50/s"),
    ("mldsa44-n250-u50-zipf-cap9984", "250 names, 50/s, Zipf 1.1"),
    ("mldsa44-n250-u50-cap1024", "250 names, 50/s, 1,024 slots"),
]
COALESCENCE = [
    ("mldsa44-no-signature-cache", "ML-DSA-44, 32 names, 100 q/s"),
    ("mldsa44-nocache-1name-q10", "ML-DSA-44, 1 name, 10 q/s"),
    ("sphincs128s-nocache-1name-q2", "SPHINCS+, 1 name, 2 q/s"),
    ("sphincs128s-nocache-1name-q10", "SPHINCS+, 1 name, 10 q/s"),
]
LOAD = [
    ("falcon512-reference", "falcon512-q400", "Falcon-512, UDP"),
    ("mldsa44-reference", "mldsa44-q400", "ML-DSA-44, UDP then TCP"),
    ("mldsa44-reused-tcp", "mldsa44-reused-tcp-q400", "ML-DSA-44, persistent TCP"),
]
DELAY = [
    ("Falcon-512, UDP", ["falcon512-reference", "falcon512-delay1", "falcon512-delay10"]),
    ("ML-DSA-44, UDP then TCP", ["mldsa44-reference", "mldsa44-delay1", "mldsa44-delay10"]),
    (
        "ML-DSA-44, persistent TCP",
        ["mldsa44-reused-tcp", "mldsa44-reused-tcp-delay1", "mldsa44-reused-tcp-delay10"],
    ),
]
LOSS = [
    ("falcon512-reference", "falcon512-loss1", "Falcon-512, UDP"),
    ("mldsa44-reference", "mldsa44-loss1", "ML-DSA-44, UDP then TCP"),
    ("mldsa44-reused-tcp", "mldsa44-reused-tcp-loss1", "ML-DSA-44, persistent TCP"),
]
ROLLOUT = [
    ("mldsa44-reference", "mldsa44-rollout", "ML-DSA-44"),
    ("sphincs128s-u1", "sphincs128s-rollout", "SPHINCS+"),
]
COLLAPSE_ANSWERED = 0.5  # fewer answers within 1 s than this marks a collapsed run


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def _fmt(value, digits, scale=1.0):
    return "n/a" if value is None else f"{scale * value:.{digits}f}"


def _table(path: Path, caption: str, label: str, spec: str, header: list[str], rows, wide=False):
    env = "table*" if wide else "table"
    lines = [
        rf"\begin{{{env}}}[t]",
        r"\centering",
        rf"\caption{{{caption}}}",
        rf"\label{{{label}}}",
    ]
    lines += [r"\small", rf"\begin{{tabular}}{{{spec}}}", r"\hline", *header, r"\hline"]
    lines += [" & ".join(row) + r" \\" for row in rows]
    lines += [r"\hline", r"\end{tabular}", rf"\end{{{env}}}"]
    path.write_text("\n".join(lines) + "\n")


def export_final(campaign: Path, output: Path, reference: Path | None = None) -> dict:
    from .figures import plot_final_capacity, plot_final_delay, plot_final_survey

    validate_paper_campaign(campaign)
    summaries = report(campaign)
    validate_verified_answers(summaries)
    by_cell: dict[str, list[dict]] = {}
    for row in summaries:
        by_cell.setdefault(row["cell_id"], []).append(row)
    for rows in by_cell.values():
        rows.sort(key=lambda row: row["repetition"])
    runs = {cell: [Path(row["run"]) for row in rows] for cell, rows in by_cell.items()}
    output.mkdir(parents=True, exist_ok=True)

    def med(cell, field, scale=1.0):
        value = _median(r.get(field) for r in by_cell[cell])
        return None if value is None else scale * value

    numbers: dict = {
        "runs": len(summaries),
        "offered": sum(r["offered"] for r in summaries),
        "verified": sum(r["verified_positive"] or 0 for r in summaries),
        "signed_unverified": sum(
            r["positive_unverified"] - (r["verified_positive"] or 0)
            for r in summaries
            if r["verified_positive"] is not None
        ),
        "updates": sum(r.get("updates_acknowledged") or 0 for r in summaries),
        "host_power_available": all(r.get("host_package_power_w") is not None for r in summaries),
    }

    # Per-run model and outcome values shared by several tables.
    derived: dict[str, list[dict]] = {}
    for cell, rows in by_cell.items():
        derived[cell] = []
        for row, run in zip(rows, runs[cell], strict=True):
            model = analysis.predicted_cache_rates(row)
            derived[cell].append(
                {
                    "answered_1s": analysis.answered_fraction(run, 1000),
                    "fresh_1s": row["fresh_on_time_1000ms"],
                    "stale": row["stale_fraction"],
                    "model_signs": model["signs_per_s"],
                    "model_signs_per_pod": model["signs_per_pod"],
                    "model_stale": model["stale_fraction"],
                    "cpu_share": row["cpu_cores"] / (float(row["cpu_limit"]) * row["replicas"])
                    if row["cpu_cores"] is not None
                    else None,
                }
            )
    sign_ms = {
        "SPHINCS+-SHA2-128s-simple": med("sphincs128s-u1", "sign_wall_mean_ms"),
        "ML-DSA-44": med("mldsa44-reference", "sign_wall_mean_ms"),
    }
    numbers["sign_ms"] = sign_ms

    # Algorithm survey: sizes, transport, signing time, CPU and exchange time.
    survey = {}
    for cell in SURVEY:
        rows = by_cell[cell]
        sizes = analysis.algorithm_sizes(runs[cell][0])
        survey[cell] = {
            "algorithm": LABELS[rows[0]["algorithm"]],
            "public_key": sizes["public_key"],
            "signature": sizes["signature_median"],
            "answer": med(cell, "final_wire_p50_bytes"),
            "tcp_fraction": med(cell, "fallback_fraction"),
            "sign_ms": med(cell, "sign_wall_mean_ms"),
            "cpu_percent": med(cell, "cpu_cores", 100),
            "cpu_runs": [100 * r["cpu_cores"] for r in rows if r["cpu_cores"] is not None],
            "exchange_p50_ms": med(cell, "wire_p50_ms"),
            "exchange_p99_ms": med(cell, "wire_p99_ms"),
            "exchange_runs": [r["wire_p99_ms"] for r in rows],
            "power_w": med(cell, "host_package_power_w"),
        }
    numbers["survey"] = survey
    _table(
        output / "table_survey.tex",
        "Algorithms at the reference configuration: 100 queries/s, 32 names, one update/s, "
        "signature cache, fresh UDP-to-TCP fallback. Public key and median signature in bytes "
        "come from the saved DNSKEY and captured RRSIGs; answer is the median complete DNS "
        "message. Signing is the plugin's mean time per signature; CPU is CoreDNS process CPU in "
        "percent of one core. Medians of five runs.",
        "tab:survey",
        "@{}lrrrlrrr@{}",
        [r"Algorithm & Key & Sig. & Answer & Path & Signing (ms) & CPU (\%) & Exch. P99 (ms) \\"],
        [
            [
                v["algorithm"],
                "--" if v["public_key"] is None else f"{v['public_key']}",
                "--" if v["signature"] is None else f"{v['signature']:.0f}",
                _fmt(v["answer"], 0),
                "TCP" if (v["tcp_fraction"] or 0) > 0.5 else "UDP",
                "--" if v["sign_ms"] is None else f"{v['sign_ms']:.3f}",
                _fmt(v["cpu_percent"], 2),
                _fmt(v["exchange_p99_ms"], 2),
            ]
            for v in survey.values()
        ],
        wide=True,
    )

    # Signing model across workloads (Eq. 1 on the logged updates).
    model_rows = []
    for cell, label in MODEL:
        predictions = [analysis.predicted_signs(run) for run in runs[cell]]
        values = {
            "updates": sum(p["updates"] for p in predictions),
            "predicted": sum(p["predicted"] for p in predictions),
            "measured": sum(p["measured"] for p in predictions)
            if all(p["measured"] is not None for p in predictions)
            else None,
            "max_abs_error": max(
                (
                    abs(p["predicted"] - p["measured"])
                    for p in predictions
                    if p["measured"] is not None
                ),
                default=None,
            ),
            "measured_runs": sum(p["measured"] is not None for p in predictions),
            "evictions": sum(r["signature_cache_evictions"] for r in by_cell[cell])
            if all(r["signature_cache_evictions"] is not None for r in by_cell[cell])
            else None,
            "hit_fraction": med(cell, "signature_cache_hit_fraction"),
            "cpu_percent": med(cell, "cpu_cores", 100),
            "exchange_p99_ms": med(cell, "wire_p99_ms"),
        }
        numbers.setdefault("model", {})[cell] = values
        model_rows.append(
            [
                label,
                f"{values['updates']}",
                f"{values['predicted']:.1f}",
                _fmt(values["measured"], 0),
                _fmt(values["evictions"], 0),
                _fmt(values["hit_fraction"], 1, 100),
            ]
        )
    _table(
        output / "table_model.tex",
        "Signing operations summed over five runs, with the prediction of "
        "Eq.~(\\ref{eq:pmiss}) applied per name to the logged update times. The prediction "
        "assumes no eviction. Hits are signature-cache hits per lookup.",
        "tab:model",
        "@{}lrrrrr@{}",
        [r"Workload & Updates & Predicted & Measured & Evictions & Hits (\%) \\"],
        model_rows,
    )

    # Capacity, response-cache TTL, cores and replicas with the slow signer.
    capacity_rows = []
    for cell, label in CAPACITY:
        rows, extra = by_cell[cell], derived[cell]
        per_sign = sign_ms[rows[0]["algorithm"]]
        demand = [
            d["model_signs_per_pod"] * per_sign / 1000 / float(r["cpu_limit"])
            for r, d in zip(rows, extra, strict=True)
        ]
        values = {
            "updates_per_s": _median(r["updates_acknowledged"] / r["duration_s"] for r in rows),
            "model_signs": _median(d["model_signs"] for d in extra),
            "signs": med(cell, "signs_per_s"),
            "model_demand": _median(demand),
            "cpu_share": _median(d["cpu_share"] for d in extra),
            "answered_1s": _median(d["answered_1s"] for d in extra),
            "fresh_1s": _median(d["fresh_1s"] for d in extra),
            "stale": _median(d["stale"] for d in extra),
            "model_stale": _median(d["model_stale"] for d in extra),
            "collapsed_runs": sum(d["answered_1s"] < COLLAPSE_ANSWERED for d in extra),
            "runs": len(rows),
            "p99_ms": med(cell, "p99_ms"),
            "power_w": med(cell, "host_package_power_w"),
            "inflight": _median(analysis.mean_inflight(run) for run in runs[cell]),
            "sign_wall_ms": med(cell, "sign_wall_mean_ms"),
        }
        numbers.setdefault("capacity", {})[cell] = values
        capacity_rows.append(
            [
                label,
                _fmt(values["model_signs"], 2),
                _fmt(values["signs"], 2),
                _fmt(values["model_demand"], 0, 100),
                _fmt(values["cpu_share"], 0, 100),
                _fmt(values["answered_1s"], 1, 100),
                _fmt(values["model_stale"], 1, 100),
                _fmt(values["stale"], 1, 100),
                f"{values['collapsed_runs']}/{values['runs']}",
            ]
        )
    _table(
        output / "table_capacity.tex",
        "Signing capacity and response caching at 100 queries/s. Medians over five runs. "
        "Model columns apply Eq.~(\\ref{eq:pmiss}) and its response-cache form to the realized "
        "update rate; demand multiplies the predicted signing rate per pod by the measured time "
        "per signature and divides by the pod quota. This demand is a wall-time proxy, not a CPU prediction. Answered counts verified answers within "
        "1~s and stale counts valid answers older than the acknowledged endpoint state, both "
        "as a share of offered queries. A collapsed run answered fewer than half of its queries "
        "within 1~s.",
        "tab:capacity",
        "lrrrrrrrr",
        [
            r"Configuration & \multicolumn{2}{c}{Signs/s} & \multicolumn{2}{c}{Demand / CPU (\% quota)}"
            r" & Answered & \multicolumn{2}{c}{Stale (\%)} & Collapsed \\",
            r" & model & meas. & proxy & meas. & $\le$1~s (\%) & model & meas. & runs \\",
        ],
        capacity_rows,
        wide=True,
    )

    # Concurrent misses without a signature cache (Eq. 2).
    coalescence_rows = []
    for cell, label in COALESCENCE:
        rows = by_cell[cell]
        a_s = [
            r["dns_transactions"] / r["duration_s"] / r["names"] * r["sign_wall_mean_ms"] / 1000
            for r in rows
            if r["sign_wall_mean_ms"] is not None
        ]
        predicted = [x / (1 + x) for x in a_s]
        values = {
            "a_s": _median(a_s),
            "predicted": _median(predicted),
            "measured": med(cell, "coalescence_observed"),
            "waiting_calls": [r["singleflight_coalesced"] for r in rows],
            "signs": med(cell, "signs_per_s"),
            "cpu_percent": med(cell, "cpu_cores", 100),
        }
        numbers.setdefault("coalescence", {})[cell] = values
        coalescence_rows.append(
            [
                label,
                _fmt(values["a_s"], 4),
                _fmt(values["predicted"], 2, 100),
                _fmt(values["measured"], 2, 100),
                _fmt(values["signs"], 1),
            ]
        )
    _table(
        output / "table_coalescence.tex",
        "Shared signing without a signature cache. $a_i s_i$ multiplies the lookup rate per key "
        "by the measured time per signature; the prediction is Eq.~(\\ref{eq:coalescence}). "
        "Direct-TCP controls have one DNS request per scheduled arrival; the archived-style "
        "32-name UDP/TCP control has dependent retries and is outside the Poisson assumption. "
        "Medians of five runs.",
        "tab:coalescence",
        "@{}lrrrr@{}",
        [r"Configuration & $a_i s_i$ & Predicted (\%) & Measured (\%) & Signs/s \\"],
        coalescence_rows,
    )

    # Steady load, delay, loss and rollouts against their reference cells.
    def compare(pairs, key):
        out = {}
        for base, cell, label in pairs:
            out[label] = {
                stage: {
                    "cpu_percent": med(c, "cpu_cores", 100),
                    "offered_qps": med(c, "offered_qps"),
                    "exchange_p50_ms": med(c, "wire_p50_ms"),
                    "exchange_p99_ms": med(c, "wire_p99_ms"),
                    "dispatch_p99_ms": med(c, "dispatch_p99_ms"),
                    "p99_ms": med(c, "p99_ms"),
                    "answered_1s": _median(d["answered_1s"] for d in derived[c]),
                    "retransmitted": _median(
                        r["retransmitted_queries"] / r["offered"] for r in by_cell[c]
                    ),
                    "signs": med(c, "signs_per_s"),
                    "latency": _median_dict(analysis.latency_quantiles(run) for run in runs[c]),
                    "client_cpu_percent": med(c, "client_cpu_cores", 100),
                }
                for stage, c in (("base", base), (key, cell))
            }
        return out

    numbers["load"] = compare(LOAD, "q400")
    numbers["loss"] = compare(LOSS, "loss1")
    numbers["rollout"] = compare(ROLLOUT, "rollout")
    _table(
        output / "table_conditions.tex",
        "Reference cells against four times the load, 1\\% reply loss with a 1~s UDP resend, "
        "and rollouts of eight names every 8~s. Medians of five runs; CPU in percent of one "
        "core, times in ms. Resent counts queries whose UDP query was sent more than once.",
        "tab:conditions",
        "@{}llrrrr@{}",
        [r"Condition & Configuration & CPU (\%) & Exch. P99 & P99.9 & Resent (\%) \\"],
        [
            [
                condition,
                label,
                f"{_fmt(v['base']['cpu_percent'], 2)} $\\to$ {_fmt(v[key]['cpu_percent'], 2)}",
                f"{_fmt(v['base']['exchange_p99_ms'], 2)} $\\to$ {_fmt(v[key]['exchange_p99_ms'], 2)}",
                f"{_fmt(v['base']['latency']['q0.999'], 1)} $\\to$ {_fmt(v[key]['latency']['q0.999'], 1)}",
                _fmt(v[key]["retransmitted"], 2, 100),
            ]
            for condition, key, group in (
                ("400 q/s", "q400", numbers["load"]),
                ("1\\% loss", "loss1", numbers["loss"]),
                ("Rollouts", "rollout", numbers["rollout"]),
            )
            for label, v in group.items()
        ],
        wide=True,
    )

    delay = {}
    for label, cells in DELAY:
        xs, medians, p99s = [], [], []
        for cell in cells:
            for row in by_cell[cell]:
                xs.append(row["pod_egress_delay_ms"])
                medians.append(row["wire_p50_ms"])
                p99s.append(row["wire_p99_ms"])
        slope, intercept = np.polyfit(xs, medians, 1)
        delay[label] = {
            "delay_ms": xs,
            "median_ms": medians,
            "p99_ms": p99s,
            "slope_round_trips": float(slope),
            "intercept_ms": float(intercept),
            "median_by_delay": {
                f"{d:g}": _median(m for x, m in zip(xs, medians, strict=True) if x == d)
                for d in sorted(set(xs))
            },
            "p99_by_delay": {
                f"{d:g}": _median(p for x, p in zip(xs, p99s, strict=True) if x == d)
                for d in sorted(set(xs))
            },
        }
    numbers["delay"] = delay
    _table(
        output / "table_delay.tex",
        "Median exchange time (ms) with added delay on the pod path. The slope of a "
        "least-squares line through all runs estimates the client round trips that reach the pod.",
        "tab:delay",
        "@{}lrrrr@{}",
        [r"Policy & 0~ms & 1~ms & 10~ms & Slope \\"],
        [
            [
                label,
                _fmt(v["median_by_delay"].get("0"), 2),
                _fmt(v["median_by_delay"].get("1"), 2),
                _fmt(v["median_by_delay"].get("10"), 2),
                f"{v['slope_round_trips']:.2f}",
            ]
            for label, v in delay.items()
        ],
    )

    # Energy per SPHINCS+ signature, when the host exposed its package energy counter.
    points = [
        (r["signs_per_s"], r["host_package_power_w"])
        for cell in ("sphincs128s-u1", "sphincs128s-u4")
        for r in by_cell[cell]
        if r["signs_per_s"] is not None and r.get("host_package_power_w") is not None
    ]
    if numbers["host_power_available"] and len({round(p[0]) for p in points}) >= 2:
        slope, intercept = np.polyfit([p[0] for p in points], [p[1] for p in points], 1)
        numbers["energy"] = {
            "package_power_slope_j_per_additional_sign": float(slope),
            "fitted_package_intercept_w": float(intercept),
        }

    anchors = ("mldsa44-reference", "falcon512-reference", "mldsa44-reused-tcp")
    fields = ("p99_ms", "wire_p99_ms", "wire_p50_ms", "cpu_cores", "signs_per_s")
    numbers["anchors"] = {cell: {f: med(cell, f) for f in fields} for cell in anchors}
    if reference is not None:
        ref_rows: dict[str, list[dict]] = {}
        for row in report_rows(reference):
            ref_rows.setdefault(row["cell_id"], []).append(row)
        numbers["anchors_reference"] = {
            cell: {f: _median(r.get(f) for r in ref_rows.get(cell, [])) for f in fields}
            for cell in anchors
        }

    plot_final_survey(survey, output)
    plot_final_capacity(
        {cell: by_cell[cell] for cell, _ in CAPACITY},
        {cell: derived[cell] for cell, _ in CAPACITY},
        sign_ms,
        output,
    )
    plot_final_delay(delay, output)
    (output / "final_numbers.json").write_text(json.dumps(numbers, indent=2, default=float) + "\n")
    return numbers


def _median_dict(dicts) -> dict:
    dicts = list(dicts)
    return {key: _median(d[key] for d in dicts) for key in dicts[0]} if dicts else {}


def report_rows(campaign: Path) -> list[dict]:
    """Read existing per-run summaries without rewriting another campaign."""
    rows = []
    for path in sorted(campaign.glob("runs/*/rep-*/attempt-*/summary.json")):
        if (path.parent / "COMPLETE").exists():
            rows.append(json.loads(path.read_text()))
    return rows


EXPECTED_COLLAPSE = {"sphincs128s-u12", "sphincs128s-u4-2pods-0.5core"}


def check_smoke(campaign: Path) -> list[str]:
    """Problems that must stop the main campaign; an empty list means go."""
    problems = []
    rows = report_rows(campaign)
    if not rows:
        return ["no complete smoke runs"]
    by_cell = {row["cell_id"]: row for row in rows}
    doc = yaml.safe_load((campaign / "campaign.yaml").read_text())
    doc_cells = {cell["id"] for cell in doc["cells"]}
    missing = doc_cells - set(by_cell)
    if missing:
        problems.append(f"smoke cells without a complete run: {sorted(missing)}")
    for cell, row in by_cell.items():
        run = Path(row["run"])
        signed = row["algorithm"] != "Baseline"
        if signed and row["positive_unverified"] != row["verified_positive"]:
            problems.append(f"{cell}: positive answers failed verification")
        if row.get("pod_egress_delay_ms") or row.get("pod_egress_loss_pct"):
            netem = run / "netem.json"
            if not netem.exists() or "netem" not in netem.read_text():
                problems.append(f"{cell}: netem record missing")
            elif (
                row.get("pod_egress_delay_ms")
                and row["wire_p50_ms"] is not None
                and row["wire_p50_ms"] < row["pod_egress_delay_ms"]
            ):
                problems.append(f"{cell}: exchange time below the added delay")
        if cell not in EXPECTED_COLLAPSE:
            good = row["verified_positive"] if signed else row["positive_unverified"]
            if not good:
                problems.append(f"{cell}: no verified or unsigned-control answers")
            if not row.get("cpu_metrics_valid", False):
                problems.append(f"{cell}: missing or invalid CPU window")
        if row.get("update_rate_configured", 0) > 0 and not row.get("updates_acknowledged"):
            problems.append(f"{cell}: no acknowledged endpoint updates")
    return problems

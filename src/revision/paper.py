"""Export the final manuscript tables, figures and quoted numbers from complete raw runs."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import yaml

from .report import report

CELLS = [
    ("unsigned-reference", "Unsigned"),
    ("ed25519-reference", "Ed25519"),
    ("falcon512-reference", "Falcon-512"),
    ("mldsa44-reference", "ML-DSA-44"),
    ("mldsa44-no-signature-cache", "ML-DSA-44, no sig. cache"),
    ("mldsa44-fast-updates", "ML-DSA-44, 10 updates/s"),
    ("ed25519-burst", "Ed25519, bursts"),
    ("mldsa44-burst", "ML-DSA-44, bursts"),
    ("mldsa44-zipf", "ML-DSA-44, Zipf"),
    ("mldsa44-two-replicas", "ML-DSA-44, two replicas"),
    ("falcon512-reused-tcp", "Falcon-512, persistent TCP"),
    ("mldsa44-reused-tcp", "ML-DSA-44, persistent TCP"),
]


def validate_paper_campaign(campaign: Path) -> None:
    """Paper panels require the full declared design and a single measured build/host."""
    doc = yaml.safe_load((campaign / "campaign.yaml").read_text())
    expected = {
        (cell["id"], rep) for cell in doc["cells"] for rep in range(1, doc["repetitions"] + 1)
    }
    observed = set()
    identities = set()
    manifest = campaign / "acquisition_manifest.json"
    build = json.loads(manifest.read_text())["build"] if manifest.exists() else None
    for complete in campaign.glob("runs/*/rep-*/attempt-*/COMPLETE"):
        run = complete.parent
        config = json.loads((run / "config.json").read_text())
        key = config["cell_id"], config["repetition"]
        if key in observed:
            raise ValueError("Duplicate complete paper repetition")
        observed.add(key)
        host = json.loads((run / "host.json").read_text())
        if build is not None and host.get("build") != build:
            raise ValueError("Paper run differs from the campaign build manifest")
        identities.add(
            json.dumps(
                {
                    k: host.get(k)
                    for k in ("build", "coredns-pqc", "cpu_model", "mem_total_kib", "platform")
                },
                sort_keys=True,
            )
        )
    if observed != expected:
        raise ValueError("Paper export requires every declared cell and repetition")
    if len(identities) != 1:
        raise ValueError("Paper export cannot combine different measured builds or hosts")


def validate_verified_answers(rows: list[dict]) -> None:
    """Never publish positive-answer timing as valid evidence when signatures failed."""
    for row in rows:
        if (
            row["algorithm"] != "Baseline"
            and row["verified_positive"] != row["positive_unverified"]
        ):
            raise ValueError(
                f"Unverified or invalid signed answers in {row['cell_id']}; inspect raw verification records"
            )


ALGORITHMS = [
    # label, cell, public key bytes, maximum signature bytes (RFC 8080, liboqs, FIPS 204)
    ("Unsigned", "unsigned-reference", None, None),
    ("Ed25519", "ed25519-reference", 32, 64),
    ("Falcon-512", "falcon512-reference", 897, 752),
    ("ML-DSA-44", "mldsa44-reference", 1312, 2420),
]

SIGNING_ROWS = [
    ("Reference", ["mldsa44-reference"]),
    ("Bursts", ["mldsa44-burst"]),
    ("Zipf", ["mldsa44-zipf"]),
    ("Two replicas", ["mldsa44-two-replicas"]),
    ("10 updates/s", ["mldsa44-fast-updates"]),
]


def _median(values):
    import statistics

    return statistics.median(values)


def export_paper(campaign: Path, output: Path) -> dict:
    """Write the final manuscript tables, figures and a JSON of the quoted numbers."""
    from . import analysis
    from .figures import plot_cpu_load, plot_latency

    validate_paper_campaign(campaign)
    shown = {key for key, _ in CELLS}
    summaries = [r for r in report(campaign) if r["cell_id"] in shown]
    validate_verified_answers(summaries)
    repetitions = yaml.safe_load((campaign / "campaign.yaml").read_text())["repetitions"]
    if any(not row["cpu_metrics_valid"] for row in summaries if row["cell_id"] in shown):
        raise ValueError(
            "The paper table requires complete CPU windows; query outcomes remain in the report"
        )
    by_cell: dict[str, list[dict]] = {}
    for row in summaries:
        by_cell.setdefault(row["cell_id"], []).append(row)
    if any(len(by_cell.get(key, [])) != repetitions for key, _ in CELLS):
        raise ValueError("Paper export requires the declared repetitions per displayed cell")
    output.mkdir(parents=True, exist_ok=True)
    numbers: dict = {"cells": {}}

    def med(cell, field):
        return _median([r[field] for r in by_cell[cell]])

    for key, _ in CELLS:
        runs = by_cell[key]
        numbers["cells"][key] = {
            "p99_ms": med(key, "p99_ms"),
            "p99_min_ms": min(r["p99_ms"] for r in runs),
            "p99_max_ms": max(r["p99_ms"] for r in runs),
            "exchange_p99_ms": med(key, "wire_p99_ms"),
            "dispatch_p99_ms": med(key, "dispatch_p99_ms"),
            "cpu_percent": 100 * med(key, "cpu_cores"),
            "signs_per_s": med(key, "signs_per_s"),
            "client_cpu_percent": 100 * med(key, "client_cpu_cores"),
            "fallback_fraction": med(key, "fallback_fraction"),
            "final_wire_bytes": med(key, "final_wire_p50_bytes"),
            "first_udp_bytes": med(key, "udp_wire_p50_bytes")
            if all(r["udp_wire_p50_bytes"] is not None for r in runs)
            else None,
            "sign_wall_ms": med(key, "sign_wall_mean_ms")
            if all(r["sign_wall_mean_ms"] is not None for r in runs)
            else None,
            "coalesced": [r["singleflight_coalesced"] for r in runs],
            "hit_fraction": med(key, "signature_cache_hit_fraction")
            if all(r["signature_cache_hit_fraction"] is not None for r in runs)
            else None,
            "evictions": sum(r["signature_cache_evictions"] or 0 for r in runs),
        }
    numbers["offered"] = sum(r["offered"] for r in summaries)
    numbers["verified"] = sum(r["verified_positive"] or 0 for r in summaries)
    numbers["failures"] = sum(r["failure_or_unknown"] for r in summaries)
    numbers["stale"] = sum(r["freshness_stale"] for r in summaries)
    numbers["freshness_unknown"] = sum(r["freshness_unknown"] for r in summaries)
    numbers["updates"] = sum(r["updates_acknowledged"] for r in summaries)
    numbers["throttled_periods_max"] = max(
        (
            r["throttled_period_fraction"]
            for r in summaries
            if r["throttled_period_fraction"] is not None
        ),
        default=None,
    )
    numbers["host_available_min_gib"] = (
        min(r["host_available_min_bytes"] for r in summaries) / 2**30
    )
    numbers["swap_out_pages"] = sum(r["host_swap_out_pages"] for r in summaries)

    # Table 1: primitive sizes with measured signing time and message sizes.
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{Algorithms. Public key and maximum signature sizes come from RFC~8080~\cite{rfc8080}, liboqs~\cite{liboqs} and FIPS~204~\cite{fips204}. Signing time is the plugin's mean in the reference runs. Answer is the median complete DNS message, excluding IP and transport headers. Sizes are in bytes.}",
        r"\label{tab:algo_properties}",
        r"\footnotesize",
        r"\setlength{\tabcolsep}{4pt}",
        r"\begin{tabular}{@{}lrrrr@{}}",
        r"\hline",
        r"Algorithm & Public key & Signature & Signing (ms) & Answer \\",
        r"\hline",
    ]
    for label, cell, pk, sig in ALGORITHMS:
        values = numbers["cells"][cell]
        sign = f"{values['sign_wall_ms']:.3f}" if values["sign_wall_ms"] is not None else "--"
        lines.append(
            f"{label} & {pk if pk else '--'} & {sig if sig else '--'} & {sign} & "
            f"{values['final_wire_bytes']:.0f} \\\\"
        )
    lines += [r"\hline", r"\end{tabular}", r"\end{table}"]
    (output / "table_algorithms.tex").write_text("\n".join(lines) + "\n")

    # Table 3: model prediction of signing operations from realized update schedules.
    predictions = {
        key: [analysis.predicted_signs(run) for run in analysis.run_dirs(campaign, key)]
        for _, keys in SIGNING_ROWS
        for key in keys
    }
    numbers["signing_model"] = {
        key: {
            "updates": sum(r["updates"] for r in rows),
            "predicted": sum(r["predicted"] for r in rows),
            "measured": sum(r["measured"] for r in rows),
            "max_abs_error": max(abs(r["predicted"] - r["measured"]) for r in rows),
        }
        for key, rows in predictions.items()
    }
    others = [
        "ed25519-reference",
        "falcon512-reference",
        "falcon512-reused-tcp",
        "mldsa44-reused-tcp",
        "ed25519-burst",
    ]
    numbers["signing_model_other"] = {
        key: {
            "predicted": sum(r["predicted"] for r in rows),
            "measured": sum(r["measured"] for r in rows),
        }
        for key in others
        for rows in [[analysis.predicted_signs(run) for run in analysis.run_dirs(campaign, key)]]
    }
    lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{ML-DSA-44 signing operations summed over all declared repetitions. The prediction applies Eq.~(\ref{eq:pmiss}) per name to the logged update times. The reference uses uniform Poisson queries and one update/s.}",
        r"\label{tab:signing_model}",
        r"\footnotesize",
        r"\begin{tabular}{@{}lrrr@{}}",
        r"\hline",
        r"Workload & Updates & Predicted & Measured \\",
        r"\hline",
    ]
    for label, keys in SIGNING_ROWS:
        values = numbers["signing_model"][keys[0]]
        lines.append(
            f"{label} & {values['updates']} & {values['predicted']:.1f} & {values['measured']} \\\\"
        )
    lines += [r"\hline", r"\end{tabular}", r"\end{table}"]
    (output / "table_signing.tex").write_text("\n".join(lines) + "\n")

    # Table 4: every configuration.
    lines = [
        r"\begin{table*}[t]",
        r"\caption{Results at a configured mean of 100 queries/s. Values are medians across all declared repetitions; brackets give the range of run P99 values. Exchange time runs from the client's first write to the answer. CPU is summed over DNS pods and expressed as a percentage of one core. Query failures and signature-verification outcomes are retained in the campaign report.}",
        r"\label{tab:revision_results}",
        r"\centering",
        r"\begin{tabular}{lrrrr}",
        r"\hline",
        r"Configuration & P99 [range] (ms) & Exchange P99 (ms) & CPU (\% core) & Signs/s \\",
        r"\hline",
    ]
    for key, label in CELLS:
        v = numbers["cells"][key]
        lines.append(
            f"{label} & {v['p99_ms']:.2f} [{v['p99_min_ms']:.2f}, {v['p99_max_ms']:.2f}] & "
            f"{v['exchange_p99_ms']:.2f} & {v['cpu_percent']:.2f} & {v['signs_per_s']:.1f} \\\\"
        )
    lines += [r"\hline", r"\end{tabular}", r"\end{table*}"]
    (output / "table_results.tex").write_text("\n".join(lines) + "\n")

    # Figure: CPU against offered load in one-second intervals of the burst runs.
    samples, fits = {}, {}
    for kind in ("dns", "client"):
        for cell, label in (
            ("ed25519-burst", "Ed25519, UDP"),
            ("mldsa44-burst", "ML-DSA-44, UDP then TCP"),
        ):
            points = [
                p
                for run in analysis.run_dirs(campaign, cell)
                for p in analysis.cpu_per_second(run, kind)
            ]
            samples[(kind, label)] = points
            fits[(kind, label)] = analysis.linear_fit(points)
            high = [p[1] for p in points if p[0] > 300]
            numbers.setdefault("cpu_load", {})[f"{kind}|{cell}"] = {
                "slope_us_per_query": 1e6 * fits[(kind, label)][0],
                "intercept_percent": 100 * fits[(kind, label)][1],
                "high_phase_mean_percent": 100 * sum(high) / len(high),
                "high_phase_max_percent": 100 * max(high),
                "per_run_slopes_us": [
                    1e6 * analysis.linear_fit(analysis.cpu_per_second(run, kind))[0]
                    for run in analysis.run_dirs(campaign, cell)
                ],
            }
    plot_cpu_load(samples, fits, output)

    # Figure: exchange-time distribution and burst phases.
    exchange = {
        label: analysis.exchange_times(campaign, cell)
        for cell, label in (
            ("unsigned-reference", "Unsigned, UDP"),
            ("falcon512-reference", "Falcon-512, UDP"),
            ("mldsa44-reference", "ML-DSA-44, UDP then TCP"),
            ("mldsa44-no-signature-cache", "ML-DSA-44, no signature cache"),
            ("mldsa44-reused-tcp", "ML-DSA-44, persistent TCP"),
        )
    }
    phases = {}
    for cell, label in (("ed25519-burst", "Ed25519"), ("mldsa44-burst", "ML-DSA-44")):
        per_run = [analysis.burst_phase_p99(run) for run in analysis.run_dirs(campaign, cell)]
        for phase in ("low", "high"):
            phases[(label, phase)] = [
                (r[phase]["dispatch_p99_ms"], r[phase]["exchange_p99_ms"]) for r in per_run
            ]
            numbers.setdefault("burst_phases", {})[f"{label}|{phase}"] = phases[(label, phase)]
    numbers["exchange_median_ms"] = {k: float(v[len(v) // 2]) for k, v in exchange.items()}
    plot_latency(exchange, phases, output)
    (output / "numbers.json").write_text(json.dumps(numbers, indent=2, default=float) + "\n")
    return numbers


def _grouped_runs(campaign: Path) -> dict[str, list[dict]]:
    rows = list(csv.DictReader((campaign / "tables/run_summaries.csv").open()))
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["cell_id"], []).append(row)
    return groups


def _control_values(
    groups: dict[str, list[dict]], keys: list[tuple[str, str]], field: str, scale: float
) -> list[list[float]]:
    return [[float(row[field]) * scale for row in groups[key]] for key, _ in keys]


def export_controls(ttl_campaign: Path, stress_campaign: Path, output: Path) -> None:
    """Plot freshness and quota sensitivity without selecting a run."""
    report(ttl_campaign)
    report(stress_campaign)
    ttl = _grouped_runs(ttl_campaign)
    stress = _grouped_runs(stress_campaign)
    ttl_keys = [
        ("mldsa44-response-off", "No response\ncache"),
        ("mldsa44-response-ttl1", "Response\n1 s"),
        ("mldsa44-response-ttl5", "Response\n5 s"),
        ("mldsa44-no-sig-cache-response-ttl1", "Response 1 s\nno sig. cache"),
    ]
    stress_keys = [
        ("mldsa44-300-one-core", "One pod\n1 core"),
        ("mldsa44-300-two-half-cores", "Two pods\n0.5 core each"),
        ("mldsa44-300-two-cores", "Two pods\n1 core each"),
    ]
    for groups, keys in ((ttl, ttl_keys), (stress, stress_keys)):
        if any(len(groups.get(key, [])) != 3 for key, _ in keys):
            raise ValueError("Control export requires three complete runs per cell")

    import matplotlib.pyplot as plt

    from .figures import CACHE_BLUE, UNCACHED_GREEN, bar_points

    fig, axes = plt.subplots(1, 3, figsize=(10.5, 3.4))
    ttl_labels = [label for _, label in ttl_keys]
    stress_labels = [label for _, label in stress_keys]
    bar_points(
        axes[0],
        _control_values(ttl, ttl_keys, "stale_post_ack_fraction", 100),
        ttl_labels,
        [CACHE_BLUE] * 3 + [UNCACHED_GREEN],
        "Signed stale answers (%)",
        "(a) Legacy cache interaction",
    )
    bar_points(
        axes[1],
        _control_values(stress, stress_keys, "p99_ms", 1),
        stress_labels,
        [UNCACHED_GREEN] * 3,
        "P99 (ms)",
        "(b) 300 queries/s",
    )
    bar_points(
        axes[2],
        _control_values(stress, stress_keys, "throttled_period_fraction", 100),
        stress_labels,
        [UNCACHED_GREEN] * 3,
        "CPU periods throttled (%)",
        "(c) 300 queries/s",
    )
    fig.tight_layout()
    fig.savefig(output / "revision_controls.pdf")
    fig.savefig(output / "revision_controls.png", dpi=180)
    plt.close(fig)

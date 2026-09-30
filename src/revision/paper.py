"""Export the compact manuscript table and figure from complete raw runs."""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

from .report import report

CELLS = [
    ("unsigned-reference", "Unsigned"),
    ("ed25519-reference", "Ed25519"),
    ("falcon512-reference", "Falcon-512"),
    ("mldsa44-reference", "ML-DSA-44"),
    ("mldsa44-no-signature-cache", "ML-DSA-44, no sig. cache"),
    ("mldsa44-fast-updates", "ML-DSA-44, 10 updates/s"),
]


def _fmt(value: str, digits: int) -> str:
    return f"{float(value):.{digits}f}" if value not in ("", "None") else "n/a"


def export_paper(campaign: Path, output: Path) -> None:
    summaries = report(campaign)
    if any(not row["cpu_metrics_valid"] for row in summaries):
        raise ValueError(
            "The historical paper table requires complete CPU windows; query outcomes remain in the report"
        )
    rows = {
        r["cell_id"]: r for r in csv.DictReader((campaign / "tables/cell_summaries.csv").open())
    }
    required_cells = {key for key, _ in CELLS} | {
        "mldsa44-burst",
        "mldsa44-zipf",
        "mldsa44-two-replicas",
        "mldsa44-reused-tcp",
    }
    if any(
        key not in rows or int(rows[key]["repetitions_complete"]) != 3 for key in required_cells
    ):
        raise ValueError("Paper export requires three complete runs per displayed cell")
    output.mkdir(parents=True, exist_ok=True)
    lines = [
        r"\begin{table*}[t]",
        r"\caption{\textcolor{blue}{Follow-up results at 100 offered queries/s and 32 headless names. Values are medians of three 30 s runs; brackets give the range of run P99 values. The unsigned row has no signature verification. Process CPU is summed over DNS pods.}}",
        r"\label{tab:revision_results}",
        r"\centering",
        r"\begin{tabular}{lrrrr}",
        r"\hline",
        r"\textcolor{blue}{Cell} & \textcolor{blue}{P99 [range] (ms)} & \textcolor{blue}{CPU (cores)} & \textcolor{blue}{Signs/s} & \textcolor{blue}{UDP to TCP (\%)} \\",
        r"\hline",
    ]
    for key, label in CELLS:
        r = rows[key]
        p99 = f"{_fmt(r['p99_ms_median'], 1)} [{_fmt(r['p99_ms_min'], 1)}, {_fmt(r['p99_ms_max'], 1)}]"
        cpu = _fmt(r["cpu_cores_median"], 3)
        signs = _fmt(r["signs_per_s_median"], 1)
        fallback = _fmt(str(float(r["fallback_fraction_median"]) * 100), 0)
        lines.append(
            rf"\textcolor{{blue}}{{{label}}} & \textcolor{{blue}}{{{p99}}} & \textcolor{{blue}}{{{cpu}}} & \textcolor{{blue}}{{{signs}}} & \textcolor{{blue}}{{{fallback}}} \\"
        )
    lines.extend([r"\hline", r"\end{tabular}", r"\end{table*}"])
    (output / "revision_results_table.tex").write_text("\n".join(lines) + "\n")
    for name in ("revision_contrasts", "revision_wire_sizes", "revision_sensitivity"):
        shutil.copyfile(campaign / f"figures/{name}.pdf", output / f"{name}.pdf")


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

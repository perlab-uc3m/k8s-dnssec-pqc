"""Curated manuscript export after export_final; retain every complete repetition."""

from __future__ import annotations

import argparse
import gzip
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import NullFormatter, ScalarFormatter

from .final import LABELS, MODEL, SURVEY, _table
from .model_checks import export_model_checks
from .paper import validate_paper_campaign, validate_verified_answers


def export_review(campaign: Path, output: Path):
    validate_paper_campaign(campaign)
    rows = []
    for complete in sorted(campaign.glob("runs/*/rep-*/attempt-*/COMPLETE")):
        run = complete.parent
        row = json.loads((run / "summary.json").read_text())
        row["run"] = str(run.relative_to(campaign))
        with gzip.open(run / "queries.jsonl.gz", "rt") as f:
            queries = [json.loads(line) for line in f]
        good = [q for q in queries if q["status"] == "positive_unverified"]
        row["answered_1s"] = sum(q["latency_ms"] <= 1000 for q in good) / len(queries)
        row["p999_ms"] = (
            float(np.quantile([q["latency_ms"] for q in good], 0.999)) if good else None
        )
        rows.append(row)
    validate_verified_answers(rows)
    by = {
        c: sorted([r for r in rows if r["cell_id"] == c], key=lambda r: r["repetition"])
        for c in sorted({r["cell_id"] for r in rows})
    }
    assert len(rows) == 280 and len(by) == 56 and all(len(v) == 5 for v in by.values())
    output.mkdir(parents=True, exist_ok=True)

    def vals(c, f, scale=1):
        return [r[f] * scale for r in by[c] if r.get(f) is not None]

    def med(c, f, scale=1):
        v = vals(c, f, scale)
        return statistics.median(v) if v else None

    totals = {
        k: sum(r.get(k) or 0 for r in rows)
        for k in [
            "offered",
            "positive_unverified",
            "verified_positive",
            "timed_out",
            "client_rejected",
            "transport_errors",
            "freshness_fresh",
            "freshness_stale",
            "freshness_unknown",
            "updates_acknowledged",
            "update_deadline_missed",
            "update_errors",
            "host_swap_out_pages",
        ]
    }
    totals["invalid_cpu_runs"] = [r["run"] for r in rows if not r["cpu_metrics_valid"]]
    totals["swap_out_runs"] = [
        {"run": r["run"], "pages": r["host_swap_out_pages"]}
        for r in rows
        if r["host_swap_out_pages"]
    ]
    totals["min_available_gib"] = min(r["host_available_min_bytes"] for r in rows) / 2**30
    fields = [
        "cpu_cores",
        "signs_per_s",
        "sign_wall_mean_ms",
        "wire_p50_ms",
        "wire_p99_ms",
        "p99_ms",
        "p999_ms",
        "answered_1s",
        "fresh_on_time_1000ms",
        "stale_fraction",
        "host_package_power_w",
        "throttled_period_fraction",
        "dispatch_p99_ms",
        "client_cpu_cores",
    ]
    numbers = {
        "totals": totals,
        "cells": {
            c: {f: {"median": med(c, f), "n": len(vals(c, f)), "runs": vals(c, f)} for f in fields}
            for c in by
        },
    }
    (output / "review_numbers.json").write_text(json.dumps(numbers, indent=2) + "\n")
    (output / "review_runs.json").write_text(json.dumps(rows, indent=2) + "\n")
    export_model_checks(rows, output)
    categories = {
        "Falcon-512": 1,
        "Falcon-1024": 5,
        "ML-DSA-44": 2,
        "ML-DSA-65": 3,
        "ML-DSA-87": 5,
        "MAYO-1": 1,
        "MAYO-3": 3,
        "SNOVA_24_5_4": 1,
        "SPHINCS+-SHA2-128s-simple": 1,
    }
    table = []
    for c in SURVEY:
        r = by[c][0]
        table.append(
            [
                LABELS[r["algorithm"]],
                str(categories.get(r["algorithm"], "--")),
                f"{med(c, 'final_wire_p50_bytes'):.0f}",
                "TCP" if med(c, "fallback_fraction") > 0.5 else "UDP",
                "--"
                if med(c, "sign_wall_mean_ms") is None
                else f"{med(c, 'sign_wall_mean_ms'):.3f}",
                f"{med(c, 'cpu_cores', 100):.2f}",
                f"{med(c, 'wire_p99_ms'):.2f}",
            ]
        )
    _table(
        output / "table_algorithms.tex",
        "Reference workload, with a signature cache and one endpoint update/s. Answer is the complete DNS message in bytes; TCP means UDP truncation followed by a fresh TCP connection. Signing is mean wall time per operation. CPU is percent of one core. Entries are medians of five run statistics. Cat. is the claimed PQC security category in the pinned liboqs metadata~\\cite{liboqs}; it is not assigned to the classical schemes or unsigned control.",
        "tab:survey",
        "@{}lcrlrrr@{}",
        [r"Algorithm & Cat. & Answer & Path & Sign (ms) & CPU (\%) & Exchange P99 (ms) \\"],
        table,
        wide=True,
    )
    final = json.loads((campaign / "export/final/final_numbers.json").read_text())
    labels = [
        "Reference",
        "10 updates/s",
        "50 updates/s",
        "Zipf, 32 names",
        "Bursty queries",
        "Two replicas",
        "Rollouts, ML-DSA-44",
        "Rollouts, SPHINCS+",
        r"250 names,\newline 9,984 entries",
        r"250 names, Zipf,\newline 9,984 entries",
        r"250 names,\newline 1,024 entries",
    ]
    table = []
    for (c, _), label in zip(MODEL, labels, strict=True):
        v = final["model"][c]
        table.append(
            [label, f"{v['predicted']:.1f}", f"{v['measured']:.0f}", f"{v['evictions']:.0f}"]
        )
    _table(
        output / "table_model.tex",
        "Signing counts summed over five runs. All rows use ML-DSA-44 unless stated. The conditional prediction uses logged update intervals and assumes immediate signing without eviction. The 250-name rows use 50 updates/s and 60~s windows; other windows last 30~s.",
        "tab:model",
        r"@{}>{\raggedright\arraybackslash}p{.39\columnwidth}rrr@{}",
        [r"Workload & Predicted & Measured & Evictions \\"],
        table,
    )
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 9,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )
    colors = ["#17649b", "#bc451a", "#21835a"]

    def points(ax, x, c, f, color, scale=1, marker="o"):
        y = vals(c, f, scale)
        ax.scatter(
            np.array([x] * len(y)) + np.linspace(-0.055, 0.055, len(y)),
            y,
            s=17,
            facecolors="none",
            edgecolors=color,
            alpha=0.65,
            marker=marker,
            zorder=3,
        )
        ax.plot([x - 0.085, x + 0.085], [statistics.median(y)] * 2, color=color, lw=2, zorder=4)

    def save(fig, name):
        fig.savefig(output / (name + ".pdf"), bbox_inches="tight")
        fig.savefig(output / (name + ".png"), bbox_inches="tight", dpi=180)
        plt.close(fig)

    fig, axs = plt.subplots(1, 2, figsize=(7.15, 2.7), layout="constrained")
    for suffix, label, color, updates in [
        ("", "One pod, 1 core", colors[0], [1, 4, 8, 12]),
        ("-2core", "One pod, 2 cores", colors[1], [4, 8, 12]),
    ]:
        cs = [f"sphincs128s-u{u}{suffix}" for u in updates]
        axs[0].plot(
            updates, [med(c, "fresh_on_time_1000ms", 100) for c in cs], color=color, label=label
        )
        for x, c in zip(updates, cs, strict=True):
            points(axs[0], x, c, "fresh_on_time_1000ms", color, 100)
    axs[0].set(
        xlabel="Endpoint updates/s",
        ylabel="Fresh within 1 s (% offered)",
        ylim=(-3, 104),
        xticks=[1, 4, 8, 12],
        title="(a) SPHINCS+ under increasing churn",
    )
    axs[0].legend(fontsize=8, loc="lower left")
    cs = [
        "sphincs128s-u4",
        "sphincs128s-u4-2pods-0.5core",
        "sphincs128s-u4-2core",
        "sphincs128s-u4-2pods-1core",
    ]
    for x, c in enumerate(cs):
        points(axs[1], x, c, "p99_ms", colors[0] if x < 2 else colors[1])
    axs[1].axvline(1.5, color=".7", lw=0.8)
    axs[1].set(
        xticks=range(4),
        xticklabels=[
            f"{label}\n{med(c, 'fresh_on_time_1000ms', 100):.1f}% fresh"
            for label, c in zip(["1 × 1", "2 × 0.5", "1 × 2", "2 × 1"], cs, strict=True)
        ],
        ylabel="Successful-answer P99 (ms)",
        xlabel="Pods × cores per pod; 4 updates/s",
        yscale="log",
        title="(b) Equal aggregate CPU budgets",
    )
    axs[1].set_yticks([100, 1000, 10000])
    axs[1].yaxis.set_major_formatter(ScalarFormatter())
    axs[1].yaxis.set_minor_formatter(NullFormatter())
    axs[1].tick_params(axis="x", labelsize=8)
    save(fig, "capacity")
    fig, axs = plt.subplots(1, 2, figsize=(7.15, 2.65), layout="constrained")
    for ax, prefix, label in zip(
        axs, ["mldsa44", "sphincs128s"], ["(a) ML-DSA-44", "(b) SPHINCS+"], strict=True
    ):
        cs = [prefix + "-u8", prefix + "-u8-ttl1", prefix + "-u8-ttl5"]
        for f, desc, color in [
            ("answered_1s", "Answered within 1 s", colors[0]),
            ("fresh_on_time_1000ms", "Fresh within 1 s", colors[2]),
            ("stale_fraction", "Stale (any latency)", colors[1]),
        ]:
            ax.plot([0, 1, 5], [med(c, f, 100) for c in cs], color=color, label=desc)
            for x, c in zip([0, 1, 5], cs, strict=True):
                points(ax, x, c, f, color, 100)
        ax.set(
            title=label,
            xlabel="Response-cache TTL (s)",
            ylabel="Share of offered queries (%)",
            xticks=[0, 1, 5],
            ylim=(-3, 104),
        )
    axs[0].legend(loc="center left", fontsize=7)
    save(fig, "freshness")
    fig, axs = plt.subplots(1, 2, figsize=(7.15, 2.7), layout="constrained")
    policies = [
        ("falcon512", "Falcon-512, UDP"),
        ("mldsa44", "ML-DSA-44, fallback"),
        ("mldsa44-reused-tcp", "ML-DSA-44, persistent TCP"),
    ]
    for i, (prefix, label) in enumerate(policies):
        base = prefix if prefix.endswith("tcp") else prefix + "-reference"
        cs = [base, prefix + "-delay1", prefix + "-delay10"]
        axs[0].plot(
            [0, 1, 10],
            [med(c, "wire_p50_ms") for c in cs],
            color=colors[i],
            label=label,
            marker=["o", "s", "^"][i],
            markersize=6 if i == 0 else 4,
            markerfacecolor="none",
            linestyle=["--", "-", ":"][i],
        )
        for x, c in zip([0, 1, 10], cs, strict=True):
            points(axs[0], x, c, "wire_p50_ms", colors[i], marker=["o", "s", "^"][i])
        points(axs[1], i - 0.14, base, "p999_ms", colors[i], marker="s")
        points(axs[1], i + 0.14, prefix + "-loss1", "p999_ms", colors[i])
    axs[0].set(
        xlabel="Added pod egress delay (ms)",
        ylabel="Median exchange time (ms)",
        xticks=[0, 1, 10],
        title="(a) Controlled delay",
    )
    axs[0].legend(fontsize=7)
    axs[1].set(
        xticks=range(3),
        xticklabels=["Falcon\nUDP", "ML-DSA\nfallback", "ML-DSA\npersistent"],
        ylabel="Successful-answer P99.9 (ms)",
        yscale="log",
        title="(b) Independent packet loss",
    )
    axs[1].scatter([], [], facecolors="none", edgecolors=".3", marker="s", label="No loss (left)")
    axs[1].scatter([], [], facecolors="none", edgecolors=".3", label="1% loss (right)")
    axs[1].legend(fontsize=7, loc="upper right")
    save(fig, "transport")
    print(json.dumps(totals, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export_review(args.campaign.resolve(), args.output.resolve())

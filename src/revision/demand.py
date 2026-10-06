"""Plot reference signing demand and preserve the matched CPU-quota comparison."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import NullFormatter, ScalarFormatter


def export_demand(output: Path):
    model = json.loads((output / "model_checks.json").read_text())["reference_demand"]
    summaries = json.loads((output / "review_runs.json").read_text())
    runs = [r | {"cell_id": cell} for cell, value in model.items() for r in value["runs"]]
    assert len(model) == 12 and len(runs) == 60
    assert all(len(value["runs"]) == 5 for value in model.values())
    assert all(np.isfinite(r["index"]) and 0 <= r["answered_1s"] <= 1 for r in runs)
    families = [
        ("One pod", "#17649b", "o", lambda c: "2pods" not in c and "ttl" not in c),
        ("Two pods", "#bc451a", "s", lambda c: "2pods" in c),
        ("Response cache", "#21835a", "^", lambda c: "ttl" in c),
    ]
    assert all(sum(match(r["cell_id"]) for _, _, _, match in families) == 1 for r in runs)
    below = [r for r in runs if r["index"] < 0.7]
    above = [r for r in runs if r["index"] > 0.9]
    result = {
        "scope": "Twelve SPHINCS+ churn, CPU, replica and response-TTL configurations",
        "runs": len(runs),
        "below_0_7": {
            "n": len(below),
            "min_answered_1s": min(r["answered_1s"] for r in below),
        },
        "above_0_9": {
            "n": len(above),
            "max_answered_1s": max(r["answered_1s"] for r in above),
        },
        "interpretation": "Descriptive campaign subsets, not validated operating thresholds",
        "replicas": {},
    }
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 8,
            "axes.titlesize": 9,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "pdf.fonttype": 42,
        }
    )
    fig, axes = plt.subplots(1, 2, figsize=(7.27, 3.0), layout="constrained")
    for label, color, marker, match in families:
        selected = [r for r in runs if match(r["cell_id"])]
        axes[0].scatter(
            [r["index"] for r in selected],
            [100 * r["answered_1s"] for r in selected],
            s=22,
            marker=marker,
            facecolors="none",
            edgecolors=color,
            linewidths=1,
            label=label,
        )
    assert max(r["index"] for r in runs) <= 2
    axes[0].set(
        xlim=(0, 2),
        ylim=(-3, 104),
        xticks=[0, 0.5, 1, 1.5, 2],
        xlabel=r"Reference signing demand $D_0$",
        ylabel="Answered within 1 s (% offered)",
        title="(a) Demand and delivery",
    )
    axes[0].legend(loc="upper right", fontsize=6.6, frameon=True, framealpha=1)
    cells = [
        "sphincs128s-u4",
        "sphincs128s-u4-2pods-0.5core",
        "sphincs128s-u4-2core",
        "sphincs128s-u4-2pods-1core",
    ]
    labels = []
    for x, (cell, label) in enumerate(
        zip(cells, ["1 × 1", "2 × 0.5", "1 × 2", "2 × 1"], strict=True)
    ):
        selected = [r for r in summaries if r["cell_id"] == cell]
        assert len(selected) == 5
        p99 = [r["p99_ms"] for r in selected]
        fresh = [100 * r["fresh_on_time_1000ms"] for r in selected]
        color = "#17649b" if x < 2 else "#bc451a"
        axes[1].scatter(
            x + np.linspace(-0.055, 0.055, 5), p99, s=20, facecolors="none", edgecolors=color
        )
        axes[1].plot([x - 0.085, x + 0.085], [statistics.median(p99)] * 2, color=color, lw=2)
        labels.append(f"{label}\n{statistics.median(fresh):.1f}% fresh")
        result["replicas"][cell] = {"p99_ms": p99, "fresh_within_1s_percent": fresh}
    axes[1].axvline(1.5, color=".7", lw=0.8)
    axes[1].set(
        xticks=range(4),
        xticklabels=labels,
        ylabel="Successful-answer P99 (ms)",
        xlabel="Pods × cores per pod; 4 updates/s",
        yscale="log",
        title="(b) Equal aggregate CPU budgets",
    )
    axes[1].set_yticks([100, 1000, 10000])
    axes[1].yaxis.set_major_formatter(ScalarFormatter())
    axes[1].yaxis.set_minor_formatter(NullFormatter())
    axes[1].tick_params(axis="x", labelsize=7)
    for ext in ["pdf", "png"]:
        fig.savefig(output / f"demand.{ext}", dpi=200, bbox_inches="tight")
    plt.close(fig)
    (output / "demand_summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "replicas"}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    export_demand(parser.parse_args().output.resolve())

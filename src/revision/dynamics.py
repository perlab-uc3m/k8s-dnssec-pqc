"""Interactions between endpoint timing, signing and freshness in the final campaign.

Run: python -m src.revision.dynamics RESULTS --output FIGURES
All contrasts pair repetitions. Samples are never pooled to manufacture replicates.
"""

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
from matplotlib.ticker import NullFormatter

from .freshness import VERIFIED, Freshness
from .paper import validate_paper_campaign, validate_verified_answers


def read_queries(run):
    with gzip.open(run / "queries.jsonl.gz", "rt") as f:
        return [json.loads(line) for line in f]


def paired_difference(left, right):
    if len(left) != len(right) or not left:
        raise ValueError("Paired contrasts require equal nonempty repetitions")
    return [b - a for a, b in zip(left, right, strict=True)]


def phase_means(queries, starts, duration, edges):
    """Equal-length complete rollout cycles, indexed by query first write.

    The client exchange starts at its first write. Restricting to cycles with a
    complete following eight seconds prevents a shorter last cycle changing bins.
    """
    starts = [t for t in starts if t + edges[-1] <= duration]
    bins = [[] for _ in edges[:-1]]
    for q in queries:
        if q["status"] != "positive_unverified" or q.get("sent_s") is None:
            raise ValueError("Rollout timing contrasts require complete positive observations")
        for t in starts:
            phase = q["sent_s"] - t
            if edges[0] <= phase < edges[-1]:
                i = int(np.searchsorted(edges, phase, side="right") - 1)
                bins[i].append(q["wire_latency_ms"])
                break
    return {
        "cycles": len(starts),
        "counts": [len(v) for v in bins],
        "mean_exchange_ms": [float(np.mean(v)) if v else None for v in bins],
    }


def export_dynamics(campaign, output):
    validate_paper_campaign(campaign)
    paths = {}
    rows = {}
    for marker in sorted(campaign.glob("runs/*/rep-*/attempt-*/COMPLETE")):
        run = marker.parent
        row = json.loads((run / "summary.json").read_text())
        paths.setdefault(row["cell_id"], []).append(run)
        rows.setdefault(row["cell_id"], []).append(row)
    if len(rows) != 56 or any(len(v) != 5 for v in rows.values()):
        raise ValueError("Expected all 56 cells and five repetitions")
    validate_verified_answers([r for group in rows.values() for r in group])

    def values(cell, field):
        return [r[field] for r in rows[cell]]

    pairs = [
        ("ML-DSA-44", "mldsa44-reference", "mldsa44-rollout"),
        ("SPHINCS+", "sphincs128s-u1", "sphincs128s-rollout"),
    ]
    fields = ["signs_per_s", "sign_wall_mean_ms", "wire_p99_ms"]
    result = {"rollouts": {}, "ttl_effects": {}, "stale_at_first_write": {}}
    result["batch_reference_s"] = (
        8 * statistics.median(values("sphincs128s-u1", "sign_wall_mean_ms")) / 1000
    )
    edges = np.arange(0, 8.01, 0.5)
    for label, base, roll in pairs:
        for a, b in zip(rows[base], rows[roll], strict=True):
            for k in [
                "repetition",
                "query_seed",
                "names",
                "query_rate_configured",
                "cpu_limit",
                "replicas",
                "response_cache_ttl",
                "signature_cache_capacity",
                "policy",
            ]:
                assert a[k] == b[k], (label, k)
        ratios = {
            f: [b / a for a, b in zip(values(base, f), values(roll, f), strict=True)]
            for f in fields
        }
        timecourse = []
        for run, row in zip(paths[roll], rows[roll], strict=True):
            events = [json.loads(line) for line in (run / "updates.jsonl").read_text().splitlines()]
            assert all(e["error"] is None for e in events)
            starts = {}
            for e in events:
                starts.setdefault(e["planned_s"], e["requested_s"])
            timecourse.append(
                phase_means(read_queries(run), list(starts.values()), row["duration_s"], edges)
            )
        result["rollouts"][label] = {
            "base": base,
            "rollout": roll,
            "paired_ratios": ratios,
            "median_ratios": {f: statistics.median(v) for f, v in ratios.items()},
            "phase_bin_edges_s": edges.tolist(),
            "phase_runs": timecourse,
        }
    for alg in ["mldsa44", "sphincs128s"]:
        result["ttl_effects"][alg] = {}
        for deadline in [10, 25, 50, 100, 250, 1000]:
            for a, b in zip(rows[alg + "-u8"], rows[alg + "-u8-ttl5"], strict=True):
                for k in ["repetition", "query_seed", "update_seed", "cpu_limit", "replicas"]:
                    assert a[k] == b[k], (alg, k)
            f = f"fresh_on_time_{deadline}ms"
            effects = paired_difference(values(alg + "-u8", f), values(alg + "-u8-ttl5", f))
            result["ttl_effects"][alg][str(deadline)] = {
                "paired_percentage_points": [100 * x for x in effects],
                "median_percentage_points": 100 * statistics.median(effects),
            }
    # Decompose stale positive answers by the state already acknowledged at first write.
    cells = [
        "sphincs128s-u4",
        "sphincs128s-u8",
        "sphincs128s-u12",
        "sphincs128s-u8-2core",
        "sphincs128s-u12-2core",
        "sphincs128s-u8-ttl5",
        "mldsa44-u8",
        "mldsa44-u8-ttl5",
    ]
    for cell in cells:
        cases = []
        for run, row in zip(paths[cell], rows[cell], strict=True):
            initial = json.loads((run / "initial_state.json").read_text())
            events = [json.loads(line) for line in (run / "updates.jsonl").read_text().splitlines()]
            fresh = Freshness(initial, events, True)
            counts = {
                "stale_at_completion": 0,
                "already_stale_at_first_write": 0,
                "current_at_first_write": 0,
                "unknown_at_first_write": 0,
            }
            for q in read_queries(run):
                q["_verification"] = VERIFIED
                if fresh.classify(q)[0] != "stale":
                    continue
                counts["stale_at_completion"] += 1
                old = fresh.classify(q | {"completed_s": q["sent_s"]})[0]
                if old == "stale":
                    counts["already_stale_at_first_write"] += 1
                elif old == "fresh":
                    counts["current_at_first_write"] += 1
                else:
                    counts["unknown_at_first_write"] += 1
            assert counts["stale_at_completion"] == row["freshness_stale"], run
            assert counts["stale_at_completion"] == sum(
                v for k, v in counts.items() if k != "stale_at_completion"
            )
            cases.append(counts)
        result["stale_at_first_write"][cell] = {
            "runs": cases,
            "totals": {k: sum(v[k] for v in cases) for k in cases[0]},
        }
    output.mkdir(parents=True, exist_ok=True)
    (output / "dynamics_numbers.json").write_text(json.dumps(result, indent=2) + "\n")
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 8.5,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "pdf.fonttype": 42,
        }
    )
    fig, axes = plt.subplots(
        1, 2, figsize=(7.15, 2.8), layout="constrained", gridspec_kw={"width_ratios": [1, 1.2]}
    )
    colors = ["#17649b", "#bc451a"]
    for j, (label, _base, _roll) in enumerate(pairs):
        data = result["rollouts"][label]
        for i, f in enumerate(fields):
            x = i + (j - 0.5) * 0.28
            y = data["paired_ratios"][f]
            axes[0].scatter(
                x + np.linspace(-0.045, 0.045, 5),
                y,
                s=18,
                facecolors="none",
                edgecolors=colors[j],
                label=label if i == 0 else None,
            )
            axes[0].plot([x - 0.075, x + 0.075], [statistics.median(y)] * 2, color=colors[j], lw=2)
        ys = np.array([v["mean_exchange_ms"] for v in data["phase_runs"]])
        xs = (edges[1:] + edges[:-1]) / 2
        for y in ys:
            axes[1].plot(xs, y, color=colors[j], alpha=0.22, lw=0.65)
        axes[1].plot(xs, np.median(ys, axis=0), color=colors[j], lw=1.7, label=label)
    axes[0].axhline(1, color=".5", lw=0.8, ls="--")
    axes[0].set(
        xticks=[0, 1, 2],
        xticklabels=["Signing\nrate", "Time per\nsignature", "Exchange\nP99"],
        ylabel="Rollout / independent updates",
        yscale="log",
        yticks=[0.5, 1, 2, 4, 8],
        yticklabels=["0.5", "1", "2", "4", "8"],
        ylim=(0.6, 8),
        title="(a) Effect of grouping changes",
    )
    axes[0].yaxis.set_minor_formatter(NullFormatter())
    axes[0].legend(fontsize=7, loc="upper left")
    axes[1].set(
        xlabel="Time since first update request in batch (s)",
        ylabel="Mean exchange time (ms)",
        yscale="log",
        xticks=[0, 2, 4, 6, 8],
        title="(b) Recovery after a rollout",
    )
    batch_s = result["batch_reference_s"]
    axes[1].axvline(batch_s, color=".4", ls="--", lw=0.8)
    axes[1].text(
        batch_s + 0.12,
        0.96,
        rf"$8s_0 = {batch_s:.2f}$ s",
        transform=axes[1].get_xaxis_transform(),
        fontsize=7,
        va="top",
    )
    axes[1].legend(fontsize=7)
    for ext in ["pdf", "png"]:
        fig.savefig(output / f"rollouts.{ext}", bbox_inches="tight", dpi=180)
    plt.close(fig)
    print(
        json.dumps(
            {
                "rollout_median_ratios": {
                    k: v["median_ratios"] for k, v in result["rollouts"].items()
                },
                "stale": {k: v["totals"] for k, v in result["stale_at_first_write"].items()},
            },
            indent=2,
        )
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export_dynamics(args.campaign.resolve(), args.output.resolve())

"""Pure plotting helpers for campaign reports and manuscript figures."""

from __future__ import annotations

import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

CACHE_BLUE = "#35a9d6"
UNCACHED_GREEN = "#a3ca63"
UNSIGNED_GRAY = "#aeb5ba"
INK = "#1d2a30"

REFERENCE_CELLS = [
    ("unsigned-reference", "Unsigned"),
    ("ed25519-reference", "Ed25519"),
    ("falcon512-reference", "Falcon-512"),
    ("mldsa44-reference", "ML-DSA-44"),
    ("mldsa44-no-signature-cache", "ML-DSA-44\nno sig. cache"),
    ("mldsa44-fast-updates", "ML-DSA-44\n10 updates/s"),
]


def bar_points(
    ax,
    values: list[list[float]],
    labels: list[str],
    colors: list[str],
    ylabel: str,
    title: str,
) -> None:
    """Show run medians as bars while retaining every run as a point."""
    ax.set_axisbelow(True)
    ax.grid(axis="y", color="#c5cbd0", linestyle="--", linewidth=0.6, alpha=0.75)
    for index, runs in enumerate(values):
        if not runs:
            continue
        median = statistics.median(runs)
        ax.bar(index, median, width=0.68, color=colors[index], edgecolor=INK, linewidth=0.7)
        for repetition, value in enumerate(runs):
            offset = (repetition - (len(runs) - 1) / 2) * 0.10
            ax.scatter(
                index + offset,
                value,
                s=17,
                facecolor="white",
                edgecolor=INK,
                linewidth=0.85,
                zorder=3,
            )
    ax.set_xticks(range(len(labels)), labels)
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontweight="bold")
    ax.tick_params(axis="x", labelsize=8)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def _values(by_cell: dict[str, list[dict]], key: str, field: str) -> list[float]:
    return [
        float(row[field])
        for row in sorted(by_cell[key], key=lambda row: row["repetition"])
        if row.get(field) is not None
    ]


def plot_campaign(summaries: list[dict], by_cell: dict[str, list[dict]], output: Path) -> None:
    """Create the all-run diagnostic and the focused reference panel."""
    output.mkdir(exist_ok=True)
    labels = [f"{row['cell_id']}\nrep {row['repetition']}" for row in summaries]
    positions = list(range(len(summaries)))
    fig, axes = plt.subplots(2, 1, figsize=(max(8, len(summaries) * 0.6), 7), sharex=True)
    for ax, field, ylabel in (
        (axes[0], "p99_ms", "P99 of positive answers (ms)"),
        (axes[1], "cpu_cores", "CoreDNS CPU (cores)"),
    ):
        ax.scatter(
            positions,
            [row[field] for row in summaries],
            s=20,
            facecolor=CACHE_BLUE,
            edgecolor=INK,
            linewidth=0.4,
        )
        ax.set_ylabel(ylabel)
        ax.grid(axis="y", color="#c5cbd0", linestyle="--", linewidth=0.6)
    axes[1].set_xticks(positions, labels, rotation=70, ha="right")
    fig.tight_layout()
    fig.savefig(output / "latency_cpu.pdf")
    fig.savefig(output / "latency_cpu.png", dpi=150)
    plt.close(fig)

    plot_wire_sizes(by_cell, output)
    plot_sensitivity(by_cell, output)
    selected = [(key, label) for key, label in REFERENCE_CELLS if key in by_cell]
    if not selected:
        return
    keys = [key for key, _ in selected]
    labels = [label for _, label in selected]
    colors = [
        UNSIGNED_GRAY
        if key.startswith("unsigned")
        else UNCACHED_GREEN
        if "no-signature-cache" in key
        else CACHE_BLUE
        for key in keys
    ]
    fig, axes = plt.subplots(2, 1, figsize=(8.5, 5.4), sharex=True)
    bar_points(
        axes[0],
        [_values(by_cell, key, "signs_per_s") for key in keys],
        labels,
        colors,
        "Signing operations/s",
        "(a) Signing work",
    )
    bar_points(
        axes[1],
        [_values(by_cell, key, "cpu_cores") for key in keys],
        labels,
        colors,
        "CoreDNS CPU (cores)",
        "(b) DNS process CPU",
    )
    fig.tight_layout()
    fig.savefig(output / "revision_contrasts.pdf")
    fig.savefig(output / "revision_contrasts.png", dpi=180)
    plt.close(fig)


def plot_wire_sizes(by_cell: dict[str, list[dict]], output: Path) -> None:
    cells = REFERENCE_CELLS[:4]
    if not all(key in by_cell for key, _ in cells):
        return
    fig, ax = plt.subplots(figsize=(6.2, 3.3))
    ax.set_axisbelow(True)
    ax.grid(axis="y", linestyle="--", linewidth=0.6, alpha=0.4)
    for offset, field, color, label in (
        (-0.18, "udp_wire_p50_bytes", UNCACHED_GREEN, "First UDP reply"),
        (0.18, "final_wire_p50_bytes", CACHE_BLUE, "Complete answer"),
    ):
        for i, (key, _) in enumerate(cells):
            runs = _values(by_cell, key, field)
            ax.bar(
                i + offset,
                statistics.median(runs),
                width=0.34,
                color=color,
                edgecolor=INK,
                linewidth=0.7,
                label=label if i == 0 else None,
            )
            ax.scatter(
                [i + offset + (j - 1) * 0.06 for j in range(len(runs))],
                runs,
                s=14,
                facecolor="white",
                edgecolor=INK,
                linewidth=0.7,
                zorder=3,
            )
    ax.axhline(1232, color="#bd4f24", linestyle="--", label="Advertised UDP payload: 1232 B")
    ax.set_yscale("log")
    ax.set_ylim(40, 7500)
    ax.set_xticks(range(len(cells)), [label for _, label in cells])
    ax.set_ylabel("DNS message bytes")
    ax.set_title("Received DNS response sizes", fontweight="bold")
    ax.legend(fontsize=8, loc="upper left", frameon=False)
    fig.tight_layout()
    fig.savefig(output / "revision_wire_sizes.pdf")
    fig.savefig(output / "revision_wire_sizes.png", dpi=180)
    plt.close(fig)


def plot_sensitivity(by_cell: dict[str, list[dict]], output: Path) -> None:
    cells = [
        ("mldsa44-reference", "Uniform\nPoisson"),
        ("mldsa44-burst", "Bursts"),
        ("mldsa44-zipf", "Zipf\npopularity"),
        ("mldsa44-two-replicas", "Two pods\n1 core each"),
        ("mldsa44-reused-tcp", "Persistent\nTCP"),
    ]
    if not all(key in by_cell for key, _ in cells):
        return
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 3.7))
    for ax, field, title in (
        (axes[0], "p99_ms", "(a) Complete query latency"),
        (axes[1], "dispatch_p99_ms", "(b) Client dispatch delay"),
    ):
        bar_points(
            ax,
            [_values(by_cell, key, field) for key, _ in cells],
            [label for _, label in cells],
            [CACHE_BLUE] * len(cells),
            "P99 (ms)",
            title,
        )
    fig.tight_layout()
    fig.savefig(output / "revision_sensitivity.pdf")
    fig.savefig(output / "revision_sensitivity.png", dpi=180)
    plt.close(fig)

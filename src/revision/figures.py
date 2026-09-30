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

    plot_freshness(summaries, output)
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


def plot_freshness(summaries: list[dict], output: Path, stem: str = "freshness_cpu") -> None:
    """Matched TTL/update outcomes, including service failures with missing CPU."""
    rows = [
        row
        for row in summaries
        if row.get("study") == "algorithm_freshness_interaction"
        and row["verified_positive"] is not None
    ]
    if not rows:
        return
    quotas = {row.get("cpu_limit") for row in rows}
    if len(quotas) > 1:
        for quota in sorted(quotas, key=str):
            suffix = "".join(c if c.isalnum() else "_" for c in str(quota))
            plot_freshness(
                [row for row in rows if row.get("cpu_limit") == quota],
                output,
                f"{stem}_quota_{suffix}",
            )
        return
    for field in (
        "query_rate_configured",
        "names",
        "cpu_limit",
        "replicas",
        "policy",
        "signature_cache_capacity",
        "arrival_mode",
        "popularity",
        "sampling_interval_s",
        "timeout_s",
        "max_inflight",
        "tcp_connections",
        "memory_limit",
        "dynamic_warmup",
    ):
        if len({row.get(field) for row in rows}) != 1:
            return
    updates = sorted({row["update_rate_configured"] for row in rows})
    ttls = sorted({row["response_cache_ttl"] for row in rows})
    if len(updates) < 2 or len(ttls) < 2:
        return
    styles = {
        "ED25519": ("Ed25519", UNSIGNED_GRAY, "o"),
        "ML-DSA-44": ("ML-DSA-44", CACHE_BLUE, "s"),
        "Falcon-512": ("Falcon-512", UNCACHED_GREEN, "D"),
        "SPHINCS+-SHA2-128s-simple": ("SPHINCS+ slow control", "#bf7735", "^"),
    }
    algorithms = sorted({row["algorithm"] for row in rows})
    fig, axes = plt.subplots(
        3, len(updates), figsize=(4 * len(updates), 8), squeeze=False, sharex=True, sharey="row"
    )
    legend = {}
    for column, rate in enumerate(updates):
        quick, outcomes, cpu = axes[:, column]
        missing = []
        for row in rows:
            if row["update_rate_configured"] != rate:
                continue
            algorithm = row["algorithm"]
            label, color, marker = styles.get(algorithm, (algorithm, INK, "o"))
            algorithm_offset = (algorithms.index(algorithm) - (len(algorithms) - 1) / 2) * 0.10
            repetitions = [r["repetition"] for r in rows if r["cell_id"] == row["cell_id"]]
            repetition_offset = (row["repetition"] - statistics.mean(repetitions)) * 0.025
            x = ttls.index(row["response_cache_ttl"]) + algorithm_offset + repetition_offset
            for ax, deadline in ((quick, 10), (outcomes, 50)):
                lower = row[f"fresh_on_time_{deadline}ms"]
                upper = row[f"fresh_on_time_upper_{deadline}ms"]
                if lower is not None:
                    point = ax.scatter(
                        x,
                        100 * lower,
                        color=color,
                        marker=marker,
                        edgecolor=INK,
                        linewidth=0.5,
                        s=23,
                    )
                    ax.plot([x, x], [100 * lower, 100 * upper], color=color, linewidth=1)
                    legend[label] = point
            if row["cpu_cores"] is not None:
                cpu.scatter(
                    x,
                    row["cpu_cores"],
                    color=color,
                    marker=marker,
                    edgecolor=INK,
                    linewidth=0.5,
                    s=23,
                )
            else:
                missing.append(label)
        quick.set_title(f"{rate:g} updates/s")
        quick.set_ylim(-3, 105)
        outcomes.set_ylim(-3, 105)
        cpu.set_ylim(0, 1.15 * max((r["cpu_cores"] or 0 for r in rows), default=0) or 1)
        if missing:
            cpu.text(
                0.04,
                0.95,
                f"CPU unavailable: {len(missing)} run(s)\n" + ", ".join(sorted(set(missing))),
                transform=cpu.transAxes,
                va="top",
                fontsize=8,
            )
        cpu.set_xticks(range(len(ttls)), [f"{ttl:g}" for ttl in ttls])
        cpu.set_xlabel("Response cache ceiling (s; 0 = off)")
        for ax in (quick, outcomes, cpu):
            ax.set_xlim(-0.5, len(ttls) - 0.5)
            ax.grid(axis="y", alpha=0.25)
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
    axes[0, 0].set_ylabel("Fresh, verified within 10 ms\n(% of all offered queries)")
    axes[1, 0].set_ylabel("Fresh, verified within 50 ms\n(% of all offered queries)")
    axes[2, 0].set_ylabel("CoreDNS process CPU (cores)")
    fig.legend(
        list(legend.values()),
        list(legend),
        loc="lower center",
        ncol=max(1, min(4, len(legend))),
        frameon=False,
    )
    fig.suptitle(f"CoreDNS CPU quota: {rows[0]['cpu_limit']} core(s)", fontsize=11)
    fig.tight_layout(rect=(0, 0.05, 1, 0.96))
    fig.savefig(output / f"{stem}.pdf")
    fig.savefig(output / f"{stem}.png", dpi=180)
    plt.close(fig)

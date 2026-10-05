"""Pure plotting helpers for campaign reports and manuscript figures."""

from __future__ import annotations

import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

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
    ax.set_title(title, fontweight="bold", fontsize=10)
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
    fig, axes = plt.subplots(2, 1, figsize=(7.1, 4.5), sharex=True)
    bar_points(
        axes[0],
        [_values(by_cell, key, "signs_per_s") for key in keys],
        labels,
        colors,
        "Signing operations/s",
        "(a) Signing work",
    )
    sign_max = max(
        (value for key in keys for value in _values(by_cell, key, "signs_per_s")), default=0
    )
    for index, key in enumerate(keys):
        runs = _values(by_cell, key, "signs_per_s")
        if not runs:
            continue
        axes[0].text(
            index,
            max(runs) + 0.035 * sign_max,
            f"{statistics.median(runs):.1f}",
            ha="center",
            fontsize=8,
        )
    axes[0].set_ylim(top=sign_max * 1.18 if sign_max else 1)
    bar_points(
        axes[1],
        [[100 * value for value in _values(by_cell, key, "cpu_cores")] for key in keys],
        labels,
        colors,
        "CPU (% of one core)",
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
    fig, ax = plt.subplots(figsize=(3.5, 2.8))
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
    ax.set_ylim(0, 3700)
    for i, (key, _) in enumerate(cells):
        median = statistics.median(_values(by_cell, key, "final_wire_p50_bytes"))
        ax.text(i + 0.18, median + 70, f"{median:.0f}", ha="center", fontsize=7)
    truncated = statistics.median(_values(by_cell, "mldsa44-reference", "udp_wire_p50_bytes"))
    ax.text(3 - 0.18, truncated + 70, f"{truncated:.0f}", ha="center", fontsize=7)
    ax.set_xticks(range(len(cells)), [label for _, label in cells], fontsize=7)
    ax.set_ylabel("DNS message bytes", fontsize=8)
    ax.tick_params(axis="y", labelsize=8)
    ax.set_title("Received DNS response sizes", fontweight="bold", fontsize=10)
    ax.legend(fontsize=6.5, loc="upper left", frameon=False)
    fig.tight_layout()
    fig.savefig(output / "revision_wire_sizes.pdf")
    fig.savefig(output / "revision_wire_sizes.png", dpi=180)
    plt.close(fig)


def plot_sensitivity(by_cell: dict[str, list[dict]], output: Path) -> None:
    cells = [
        ("mldsa44-reference", "Uniform\nPoisson"),
        ("mldsa44-burst", "Bursts"),
        ("mldsa44-zipf", "Zipf"),
        ("mldsa44-two-replicas", "Two pods\n1 core/pod"),
        ("mldsa44-reused-tcp", "Persistent\nTCP"),
    ]
    if not all(key in by_cell for key, _ in cells):
        return
    fig, axes = plt.subplots(1, 2, figsize=(7.1, 3.3))
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
        ax.tick_params(axis="x", labelsize=7)
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


# Final manuscript figures. Colors follow a validated categorical order
# (blue, orange, violet, aqua) with a neutral gray for the unsigned control.
F_BLUE, F_ORANGE, F_VIOLET, F_AQUA, F_GRAY = "#2a78d6", "#eb6834", "#4a3aa7", "#1baf7a", "#8a8f94"


def _final_style() -> None:
    plt.rcParams.update(
        {
            "font.size": 8,
            "axes.titlesize": 8.5,
            "axes.labelsize": 8,
            "xtick.labelsize": 7.5,
            "ytick.labelsize": 7.5,
            "legend.fontsize": 7,
            "pdf.fonttype": 42,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.linewidth": 0.6,
        }
    )


def plot_cpu_load(samples: dict, fits: dict, output: Path) -> None:
    """samples[(kind, label)] -> [(queries/s, cores)]; fits share the keys."""
    _final_style()
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.55))
    styles = {"Ed25519, UDP": (F_BLUE, "o"), "ML-DSA-44, UDP then TCP": (F_ORANGE, "s")}
    for ax, kind, title in (
        (axes[0], "dns", "(a) CoreDNS process"),
        (axes[1], "client", "(b) Load generator process"),
    ):
        for label, (color, marker) in styles.items():
            points = samples[(kind, label)]
            ax.scatter(
                [p[0] for p in points],
                [100 * p[1] for p in points],
                s=11,
                marker=marker,
                facecolor="white",
                edgecolor=color,
                linewidth=0.8,
                label=label,
                zorder=3,
            )
            slope, intercept = fits[(kind, label)]
            ax.plot(
                [0, 480], [100 * intercept, 100 * (intercept + slope * 480)], color=color, lw=1.4
            )
            ax.text(
                500,
                100 * (intercept + slope * 480),
                f"{slope * 1e6:.0f} µs/query",
                color=INK,
                fontsize=7,
                va="center",
            )
        ax.set_xlim(0, 640)
        ax.set_xticks([0, 100, 200, 300, 400, 500])
        ax.set_ylim(bottom=0)
        ax.set_xlabel("Offered queries/s in one-second interval")
        ax.set_ylabel("CPU (% of one core)")
        ax.set_title(title, loc="left", fontweight="bold")
        ax.grid(axis="y", color="#d5d9dc", lw=0.5)
        ax.set_axisbelow(True)
    axes[0].legend(frameon=False, loc="upper left")
    fig.tight_layout(w_pad=2.5)
    fig.savefig(output / "cpu_load.pdf")
    fig.savefig(output / "cpu_load.png", dpi=200)
    plt.close(fig)


def plot_latency(exchange: dict, phases: dict, output: Path) -> None:
    """exchange[label] -> sorted ms; phases[(algorithm, phase)] -> [(dispatch, exchange)] per run."""
    _final_style()
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 3.1), gridspec_kw={"width_ratios": [1.15, 1]})
    ax = axes[0]
    styles = {
        "Unsigned, UDP": (F_GRAY, "-"),
        "Falcon-512, UDP": (F_AQUA, "-"),
        "ML-DSA-44, UDP then TCP": (F_ORANGE, "-"),
        "ML-DSA-44, no signature cache": (F_ORANGE, "--"),
        "ML-DSA-44, persistent TCP": (F_VIOLET, "-"),
    }
    for label, (color, line) in styles.items():
        values = exchange[label]
        above = 1 - (np.arange(len(values)) / len(values))
        ax.step(values, above, where="post", color=color, ls=line, lw=1.3, label=label)
    ax.set_yscale("log")
    ax.set_ylim(5e-4, 1.05)
    ax.set_xlim(0, 3.0)
    ax.set_xlabel("Exchange time, first write to answer (ms)")
    ax.set_ylabel("Fraction of queries above x")
    ax.set_title("(a) Exchange time at 100 queries/s", loc="left", fontweight="bold")
    ax.grid(color="#e3e6e8", lw=0.5)
    ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.24), ncol=2, fontsize=6.5)
    ax = axes[1]
    groups = [("Ed25519", "low"), ("Ed25519", "high"), ("ML-DSA-44", "low"), ("ML-DSA-44", "high")]
    width = 0.36
    for j, (color, label) in enumerate(
        ((F_GRAY, "Client delay before first write"), (F_ORANGE, "Exchange time"))
    ):
        for i, key in enumerate(groups):
            values = [run[j] for run in phases[key]]
            x = i + (j - 0.5) * width
            ax.bar(
                x,
                statistics.median(values),
                width=width * 0.92,
                color=color,
                edgecolor=INK,
                lw=0.5,
                label=label if i == 0 else None,
            )
            offsets = [(k - (len(values) - 1) / 2) * 0.07 for k in range(len(values))]
            ax.scatter(
                [x + o for o in offsets],
                values,
                s=9,
                facecolor="white",
                edgecolor=INK,
                lw=0.6,
                zorder=3,
            )
    ax.set_xticks(
        range(4), ["Ed25519\n45 q/s", "Ed25519\n455 q/s", "ML-DSA-44\n45 q/s", "ML-DSA-44\n455 q/s"]
    )
    ax.set_ylabel("P99 (ms)")
    ax.set_title("(b) Burst runs by phase", loc="left", fontweight="bold")
    ax.grid(axis="y", color="#d5d9dc", lw=0.5)
    ax.set_axisbelow(True)
    ax.legend(frameon=False, loc="upper left", fontsize=6.5)
    fig.tight_layout(w_pad=2.0)
    fig.savefig(output / "latency.pdf")
    fig.savefig(output / "latency.png", dpi=200)
    plt.close(fig)


def plot_final_capacity(rows: dict, derived: dict, sign_ms: dict, output: Path) -> None:
    """(a) Measured against predicted CPU for SPHINCS+; (b) outcome shares with TTL."""
    _final_style()
    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.9), gridspec_kw={"width_ratios": [1, 1.25]})
    ax = axes[0]
    groups = (
        (
            "One pod, one core",
            F_BLUE,
            "o",
            lambda c: "ttl" not in c and "pods" not in c and "2core" not in c,
        ),
        ("Response cache", F_AQUA, "s", lambda c: "ttl" in c),
        ("One pod, two cores", F_ORANGE, "^", lambda c: "2core" in c),
        ("Two pods", F_VIOLET, "D", lambda c: "pods" in c),
    )
    top = 135
    for label, color, marker, test in groups:
        xs, ys, cx = [], [], []
        for cell, cell_rows in rows.items():
            if not cell.startswith("sphincs") or not test(cell):
                continue
            per_sign = sign_ms[cell_rows[0]["algorithm"]]
            for row, extra in zip(cell_rows, derived[cell], strict=True):
                demand = (
                    100 * extra["model_signs_per_pod"] * per_sign / 1000 / float(row["cpu_limit"])
                )
                if extra["answered_1s"] < 0.5:
                    cx.append(demand)
                elif extra["cpu_share"] is not None:
                    xs.append(demand)
                    ys.append(100 * extra["cpu_share"])
        ax.scatter(
            xs,
            ys,
            s=14,
            marker=marker,
            facecolor="white",
            edgecolor=color,
            lw=0.9,
            label=label,
            zorder=3,
        )
        ax.scatter(cx, [top - 8] * len(cx), s=22, marker="x", color=color, lw=1.1, zorder=3)
    ax.plot([0, 130], [0, 130], color=INK, lw=0.7, ls="--")
    ax.axhline(100, color="#bd4f24", lw=0.8)
    ax.axvline(100, color="#bd4f24", lw=0.8, ls=":")
    ax.text(3, top - 8, "collapsed", va="center", fontsize=7)
    demands = [
        100 * d["model_signs_per_pod"] * sign_ms[r["algorithm"]] / 1000 / float(r["cpu_limit"])
        for cell, cell_rows in rows.items()
        if cell.startswith("sphincs")
        for r, d in zip(cell_rows, derived[cell], strict=True)
    ]
    ax.set_xlim(0, max(200, 1.1 * max(demands, default=0)))
    ax.set_ylim(0, top)
    ax.set_xlabel("Signing demand proxy (% of pod quota)")
    ax.set_ylabel("Measured CPU (% of pod quota)")
    ax.set_title("(a) SPHINCS+ signing capacity", loc="left", fontweight="bold")
    ax.grid(color="#e3e6e8", lw=0.5)
    ax.legend(frameon=False, loc="lower right", fontsize=6.5)

    ax = axes[1]
    order = [
        ("sphincs128s-u8", "S 8/s\nTTL 0"),
        ("sphincs128s-u8-ttl1", "S 8/s\nTTL 1"),
        ("sphincs128s-u8-ttl5", "S 8/s\nTTL 5"),
        ("sphincs128s-u12", "S 12/s\nTTL 0"),
        ("sphincs128s-u12-ttl5", "S 12/s\nTTL 5"),
        ("mldsa44-u8", "M 8/s\nTTL 0"),
        ("mldsa44-u8-ttl1", "M 8/s\nTTL 1"),
        ("mldsa44-u8-ttl5", "M 8/s\nTTL 5"),
    ]
    order = [(c, label) for c, label in order if c in derived]
    fresh = [100 * statistics.median(d["fresh_1s"] for d in derived[c]) for c, _ in order]
    stale = [100 * statistics.median(d["stale"] for d in derived[c]) for c, _ in order]
    rest = [max(0.0, 100 - f - s) for f, s in zip(fresh, stale, strict=True)]
    x = range(len(order))
    ax.bar(x, fresh, color=F_BLUE, edgecolor="white", lw=0.8, label="Fresh within 1 s")
    ax.bar(x, stale, bottom=fresh, color=F_ORANGE, edgecolor="white", lw=0.8, label="Stale")
    ax.bar(
        x,
        rest,
        bottom=[f + s for f, s in zip(fresh, stale, strict=True)],
        color="#d5d9dc",
        edgecolor="white",
        lw=0.8,
        label="Late, failed or unknown",
    )
    ax.set_xticks(list(x), [label for _, label in order], fontsize=6.5)
    ax.set_ylim(0, 100)
    ax.set_ylabel("Share of offered queries (%)")
    ax.set_title("(b) Outcomes with response caching", loc="left", fontweight="bold")
    ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.2), ncol=3, fontsize=6.5)
    fig.tight_layout(w_pad=2.0)
    fig.savefig(output / "final_capacity.pdf")
    fig.savefig(output / "final_capacity.png", dpi=200)
    plt.close(fig)


def plot_final_delay(delay: dict, output: Path) -> None:
    _final_style()
    fig, ax = plt.subplots(figsize=(3.4, 2.6))
    styles = {
        "Falcon-512, UDP": (F_AQUA, "o"),
        "ML-DSA-44, UDP then TCP": (F_ORANGE, "s"),
        "ML-DSA-44, persistent TCP": (F_VIOLET, "D"),
    }
    for label, values in delay.items():
        color, marker = styles.get(label, (INK, "o"))
        ax.scatter(
            values["delay_ms"],
            values["median_ms"],
            s=12,
            marker=marker,
            facecolor="white",
            edgecolor=color,
            lw=0.8,
            zorder=3,
        )
        xs = np.array([0, 10.5])
        ax.plot(
            xs,
            values["intercept_ms"] + values["slope_round_trips"] * xs,
            color=color,
            lw=1.2,
            label=f"{label} (slope {values['slope_round_trips']:.1f})",
        )
    ax.set_xlabel("Added delay on the pod path (ms)")
    ax.set_ylabel("Median exchange time (ms)")
    ax.grid(color="#e3e6e8", lw=0.5)
    ax.legend(frameon=False, loc="upper left", fontsize=6.3)
    fig.tight_layout()
    fig.savefig(output / "final_delay.pdf")
    fig.savefig(output / "final_delay.png", dpi=200)
    plt.close(fig)


def plot_final_survey(survey: dict, output: Path) -> None:
    """Answer size, CoreDNS CPU and exchange P99 for every algorithm, by answer size."""
    _final_style()
    items = sorted(survey.values(), key=lambda v: v["answer"] or 0)
    labels = [v["algorithm"] for v in items]
    colors = [
        F_GRAY if v["sign_ms"] is None else F_ORANGE if (v["tcp_fraction"] or 0) > 0.5 else F_BLUE
        for v in items
    ]
    x = np.arange(len(items))
    fig, axes = plt.subplots(3, 1, figsize=(7.0, 5.6), sharex=True)
    panels = (
        (axes[0], [v["answer"] for v in items], None, "Answer (bytes)", "(a) Complete answer"),
        (
            axes[1],
            [v["cpu_percent"] for v in items],
            "cpu_runs",
            "CPU (% of a core)",
            "(b) CoreDNS CPU",
        ),
        (
            axes[2],
            [v["exchange_p99_ms"] for v in items],
            "exchange_runs",
            "P99 (ms)",
            "(c) Exchange time P99",
        ),
    )
    for ax, heights, runs, ylabel, title in panels:
        ax.bar(x, [h or 0 for h in heights], color=colors, edgecolor="white", lw=0.8, width=0.7)
        if runs:
            for i, v in enumerate(items):
                values = v[runs]
                offsets = [(k - (len(values) - 1) / 2) * 0.08 for k in range(len(values))]
                ax.scatter(
                    [i + o for o in offsets],
                    values,
                    s=7,
                    facecolor="white",
                    edgecolor=INK,
                    lw=0.5,
                    zorder=3,
                )
        ax.set_ylabel(ylabel)
        ax.set_title(title, loc="left", fontweight="bold")
        ax.grid(axis="y", color="#e3e6e8", lw=0.5)
        ax.set_axisbelow(True)
    axes[0].axhline(1232, color="#bd4f24", ls="--", lw=0.8)
    axes[0].text(0, 1300, "advertised UDP payload, 1232 B", fontsize=6.5, color="#bd4f24")
    for ax in axes:
        ax.set_yscale("log")
    axes[2].set_xticks(x, labels, rotation=35, ha="right", fontsize=7)
    handles = [
        plt.Rectangle((0, 0), 1, 1, color=F_BLUE),
        plt.Rectangle((0, 0), 1, 1, color=F_ORANGE),
        plt.Rectangle((0, 0), 1, 1, color=F_GRAY),
    ]
    axes[1].legend(
        handles, ["Answer over UDP", "UDP, then TCP", "Unsigned"], frameon=False, fontsize=6.5
    )
    fig.tight_layout()
    fig.savefig(output / "final_survey.pdf")
    fig.savefig(output / "final_survey.png", dpi=200)
    plt.close(fig)

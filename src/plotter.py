"""Paper-quality figure generation from benchmark results."""

import json
import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np

from src.analyzer import RunSummary, SIGNING_TIMES_MS

log = logging.getLogger(__name__)

# ── colour palette: green → blue gradient (user signature) ──────────────
_ALGO_ORDER = [
    "Falcon-512",
    "Falcon-1024",
    "ML-DSA-44",
    "ML-DSA-65",
    "ML-DSA-87",
    "MAYO-1",
    "MAYO-3",
    "SNOVA_24_5_4",
    "SPHINCS+-SHA2-128s-simple",
    "ECDSAP256SHA256",
    "ED25519",
    "RSASHA256",
]
_CMAP = mcolors.LinearSegmentedColormap.from_list("", ["#9fcf69", "#33acdc"])
_PALETTE = [_CMAP(v) for v in np.linspace(0, 1, len(_ALGO_ORDER))]

ALGO_COLORS = dict(zip(_ALGO_ORDER, _PALETTE))

ALGO_MARKERS = {
    "Falcon-512": "o",
    "Falcon-1024": "v",
    "ML-DSA-44": "s",
    "ML-DSA-65": "p",
    "ML-DSA-87": "h",
    "MAYO-1": "^",
    "MAYO-3": "<",
    "SNOVA_24_5_4": "D",
    "SPHINCS+-SHA2-128s-simple": "*",
    "ECDSAP256SHA256": "X",
    "ED25519": "P",
    "RSASHA256": "d",
}

ALGO_LABELS = {
    "Falcon-512": "FALCON-512",
    "Falcon-1024": "FALCON-1024",
    "ML-DSA-44": "ML-DSA-44",
    "ML-DSA-65": "ML-DSA-65",
    "ML-DSA-87": "ML-DSA-87",
    "MAYO-1": "MAYO-1",
    "MAYO-3": "MAYO-3",
    "SNOVA_24_5_4": "SNOVA",
    "SPHINCS+-SHA2-128s-simple": "SLH-DSA-SHA2-128s",
    "ECDSAP256SHA256": "ECDSA-P256",
    "ED25519": "Ed25519",
    "RSASHA256": "RSA-SHA256",
    "NONE": "Baseline",
}

# ── simulated-delay styling ─────────────────────────────────────────────
_SIM_COLOR = "#DAA520"  # goldenrod
_SIM_EDGE = "#8B6914"  # dark goldenrod
_SIM_MARKER_UDP = "D"  # diamond
_SIM_MARKER_TCP = "d"  # thin diamond
_SIM_SIZE = 55


def _valid_sim(s: "RunSummary") -> bool:
    """True if the run is a simulated-delay run."""
    return s.simulated_delay_ms is not None


def _sim_label(s: "RunSummary") -> str:
    """Human-readable label for a simulated-delay run, e.g. 'Ed25519 +10 ms'."""
    base = ALGO_LABELS.get(s.algorithm, s.algorithm)
    delay = (
        int(s.simulated_delay_ms)
        if s.simulated_delay_ms == int(s.simulated_delay_ms)
        else s.simulated_delay_ms
    )
    return f"{base} +{delay}\u2009ms"


def _style():
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "axes.labelsize": 12,
            "axes.labelweight": "bold",
            "axes.titlesize": 16,
            "axes.titleweight": "bold",
            "axes.titlepad": 20,
            "legend.fontsize": 10,
            "xtick.labelsize": 11,
            "ytick.labelsize": 11,
            "figure.figsize": (11, 6),
            "figure.dpi": 300,
            "savefig.dpi": 300,
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.05,
        }
    )


def _grid(ax):
    ax.grid(True, linestyle="--", which="both", color="grey", alpha=0.4)
    ax.set_axisbelow(True)


def _save(fig, output: Path):
    """Save figure as both PDF and PNG, then close."""
    fig.savefig(output)
    sibling = output.with_suffix(".png" if output.suffix == ".pdf" else ".pdf")
    fig.savefig(sibling)
    plt.close(fig)
    log.info("Saved %s + %s", output.name, sibling.name)


# ── helpers for mean ± std aggregation ──────────────────────────────────


def _load_raw_latencies(campaign_dir: Path, summary: RunSummary) -> np.ndarray:
    """Load successful query latencies from queries.jsonl for a specific run."""
    label = ALGO_LABELS.get(summary.algorithm, summary.algorithm)
    algo_dir = campaign_dir / label
    if not algo_dir.is_dir():
        return np.array([])
    # Build suffix that every matching dir must contain
    cr_qr = f"cr{summary.churn_rate}_qr{int(summary.query_rate)}"
    sc = summary.sig_cache_cap or 0
    nd = summary.network_delay_ms if summary.network_delay_ms is not None else 0
    nr = summary.network_rate_kbps
    # Find matching param dir
    for d in sorted(algo_dir.iterdir()):
        if not d.is_dir() or cr_qr not in d.name:
            continue
        # Check sig_cache_cap match
        if f"sc{sc}_" in d.name or d.name.startswith(f"sc{sc}_"):
            pass
        elif f"nr{nr}" in d.name and nr is not None:
            pass
        else:
            continue
        # Verify network params match
        if nd > 0 and f"nd{nd}" not in d.name:
            continue
        if nr is not None and nr > 0 and f"nr{nr}" not in d.name:
            continue
        path = d / "run_0" / "queries.jsonl"
        if path.exists():
            latencies = []
            with open(path) as f:
                for line in f:
                    q = json.loads(line)
                    if q["error"] is None:
                        latencies.append(q["latency_ms"])
            return np.array(latencies)
    log.warning("Could not find queries.jsonl for %s at %s", label, cr_qr)
    return np.array([])


def _group_by_sigma(runs: list[RunSummary]):
    """Group runs by rounded sigma → lists of P99 values."""
    groups: dict[float, list[float]] = {}
    for r in runs:
        if r.sigma is not None:
            key = round(r.sigma, 6)
            groups.setdefault(key, []).append(r.latency_p99)
    return groups


def _group_by_churn(runs: list[RunSummary]):
    groups: dict[float, list[float]] = {}
    for r in runs:
        groups.setdefault(r.churn_rate, []).append(r.latency_p99)
    return groups


def _baseline_p99(summaries: list[RunSummary]) -> float | None:
    """Median P99 of unsigned baseline (NONE) runs, or None."""
    vals = [s.latency_p99 for s in summaries if s.algorithm == "NONE"]
    return float(np.median(vals)) if vals else None


def _draw_baseline(ax, L0: float | None, orientation: str = "h"):
    """Draw L0 reference line if baseline data exists."""
    if L0 is None:
        return
    if orientation == "h":
        ax.axhline(
            L0,
            color="#d35400",
            linestyle="--",
            linewidth=1.3,
            alpha=0.7,
            zorder=2,
            label=f"$L_0$ baseline ({L0:.1f} ms)",
        )
    else:
        ax.axvline(
            L0,
            color="#d35400",
            linestyle="--",
            linewidth=1.3,
            alpha=0.7,
            zorder=2,
            label=f"$L_0$ baseline ({L0:.1f} ms)",
        )


def _add_linear_fit(ax, x: np.ndarray, y: np.ndarray, w: np.ndarray):
    """Add a weighted linear regression line with R² annotation.

    Works on log-transformed x when the axis uses log scale.
    """
    if len(x) < 2:
        return
    # Use log(x) to match log-scale axis
    log_x = np.log10(np.maximum(x, 1e-6))
    coeffs = np.polyfit(log_x, y, 1, w=w)
    x_fit = np.linspace(log_x.min(), log_x.max(), 200)
    y_fit = np.polyval(coeffs, x_fit)
    # R²
    y_pred = np.polyval(coeffs, log_x)
    ss_res = np.sum(w * (y - y_pred) ** 2)
    ss_tot = np.sum(w * (y - np.average(y, weights=w)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0
    ax.plot(
        10**x_fit,
        y_fit,
        color="black",
        linewidth=1.5,
        linestyle="--",
        alpha=0.6,
        zorder=6,
        label=f"Fit: $R^2 = {r2:.3f}$",
    )


def _log_safe_yerr(means, stds):
    """Convert symmetric stds into asymmetric [lo, hi] arrays safe for log scale.

    Clamps the lower error bar so that mean - lo_err > 0, avoiding negative
    values that matplotlib cannot display on a logarithmic axis.
    """
    means_a = np.asarray(means, dtype=float)
    stds_a = np.asarray(stds, dtype=float)
    lo = np.where(means_a - stds_a > 0, stds_a, means_a * 0.9)
    hi = stds_a
    return [lo, hi]


# ── plot functions ──────────────────────────────────────────────────────


def plot_latency_vs_sigma(summaries: list[RunSummary], output: Path):
    """Data-collapse plot: P99 latency vs cryptographic load Sigma.

    Each point is a single run.  Marker size encodes the caching
    configuration; all points are solid-filled with algorithm colour
    and a thick black edge.
    Simulated-delay runs use diamond markers sized by delay magnitude.
    """
    _style()
    fig, ax = plt.subplots()

    # Cache-config visual encoding: (sc, ttl) → (size, label)
    _CACHE_SIZE: dict[tuple, tuple[int, str]] = {
        (0, 0): (120, "No cache (sc\u20090, TTL\u20090)"),
        (0, 5): (80, "TTL only (sc\u20090, TTL\u20095)"),
        (10000, 0): (55, "Sig-cache (sc\u200910k, TTL\u20090)"),
        (10000, 5): (35, "Full cache (sc\u200910k, TTL\u20095)"),
    }

    by_algo: dict[str, list[RunSummary]] = {}
    for s in summaries:
        if (
            s.sigma is not None
            and s.simulated_delay_ms is None
            and s.network_delay_ms in (None, 0)
            and s.network_rate_kbps is None
        ):
            by_algo.setdefault(s.algorithm, []).append(s)

    # Plot each point individually with size encoding
    plotted_algos: set[str] = set()
    for algo in _ALGO_ORDER:
        if algo not in by_algo:
            continue
        color = ALGO_COLORS[algo]
        for r in sorted(by_algo[algo], key=lambda r: r.sigma):
            key = ((r.sig_cache_cap or 0), int(r.cache_ttl or 0))
            size, _ = _CACHE_SIZE.get(key, (80, "other"))
            label = ALGO_LABELS.get(algo, algo) if algo not in plotted_algos else None
            plotted_algos.add(algo)
            ax.scatter(
                r.sigma,
                r.latency_p99,
                facecolors=color,
                edgecolors="black",
                marker=ALGO_MARKERS[algo],
                linewidth=0.7,
                s=size,
                alpha=0.75,
                zorder=5,
                label=label,
            )

    ax.set_xlabel(r"Cryptographic load $\Sigma$", labelpad=10)
    ax.set_ylabel("P99 latency (ms)", labelpad=20)
    ax.set_xscale("log")
    ax.set_yscale("log")

    # ── simulated-delay overlay (diamond, size proportional to delay) ───
    from src.analyzer import TRANSPORT_OVERHEAD_MS

    sim_runs = [
        s
        for s in summaries
        if _valid_sim(s)
        and s.sigma is not None
        and s.network_delay_ms in (None, 0)
        and s.network_rate_kbps is None
    ]
    sim_runs.sort(key=lambda s: (s.algorithm, s.simulated_delay_ms))
    _SIM_SIZE_MAP = {1: 30, 10: 60, 50: 110, 200: 180}
    for s in sim_runs:
        delay = int(s.simulated_delay_ms)
        sz = _SIM_SIZE_MAP.get(delay, 55)
        is_tcp = TRANSPORT_OVERHEAD_MS.get(s.algorithm, 0) > 0
        ax.scatter(
            s.sigma,
            s.latency_p99,
            color=_SIM_COLOR,
            marker=_SIM_MARKER_TCP if is_tcp else _SIM_MARKER_UDP,
            s=sz,
            edgecolor=_SIM_EDGE,
            linewidth=0.7,
            alpha=0.75,
            zorder=4,
        )

    ax.axvline(x=1, color="grey", linestyle=":", linewidth=1.5, alpha=0.7)
    ax.text(
        1.15,
        ax.get_ylim()[0] * 1.3 if ax.get_ylim()[0] > 0 else 3,
        r"$\Sigma = 1$",
        color="grey",
        fontsize=10,
        ha="left",
        va="bottom",
    )
    _draw_baseline(ax, _baseline_p99(summaries))

    # ── shaded regions for caching regimes ──────────────────────────────
    _REGIME_SHADING = {
        (0, 0): ("#FFE0E0", "No cache"),
        (0, 5): ("#FFFAE0", "TTL only"),
        (10000, 0): ("#E0F0FF", "Sig-cache"),
        (10000, 5): ("#E0FFE0", "Full cache"),
    }
    # Collect Sigma ranges per regime from real (non-simulated) data
    regime_ranges: dict[tuple, list[float]] = {}
    for s in summaries:
        if (
            s.sigma is not None
            and s.sigma > 0
            and s.simulated_delay_ms is None
            and s.network_delay_ms in (None, 0)
            and s.network_rate_kbps is None
        ):
            key = ((s.sig_cache_cap or 0), int(s.cache_ttl or 0))
            regime_ranges.setdefault(key, []).append(s.sigma)
    for key, sigmas in sorted(regime_ranges.items(), key=lambda x: min(x[1])):
        if key not in _REGIME_SHADING:
            continue
        color, label = _REGIME_SHADING[key]
        lo, hi = min(sigmas) * 0.7, max(sigmas) * 1.4
        ax.axvspan(lo, hi, alpha=0.18, color=color, zorder=0)
        y_pos = ax.get_ylim()[1] * 0.7
        ax.text(
            np.sqrt(lo * hi),
            y_pos,
            label,
            fontsize=7,
            ha="center",
            va="top",
            color="#555555",
            fontstyle="italic",
            zorder=1,
        )

    # ── legend below the plot ───────────────────────────────────────────
    from matplotlib.lines import Line2D

    handles, _ = ax.get_legend_handles_labels()

    # Cache-config size legend (grey circles, increasing size)
    for (_sc, _ttl), (sz, lbl) in _CACHE_SIZE.items():
        handles.append(
            Line2D(
                [],
                [],
                marker="o",
                linestyle="none",
                markerfacecolor="grey",
                markeredgecolor="black",
                markeredgewidth=0.7,
                alpha=0.75,
                markersize=2.5 + sz / 18,
                label=lbl,
            )
        )

    # Simulated size legend
    if sim_runs:
        for delay, sz in sorted(_SIM_SIZE_MAP.items()):
            handles.append(
                Line2D(
                    [],
                    [],
                    marker=_SIM_MARKER_UDP,
                    color=_SIM_COLOR,
                    markeredgecolor=_SIM_EDGE,
                    linestyle="none",
                    markersize=3 + sz / 25,
                    label=f"Simulated +{delay}\u2009ms",
                )
            )

    ncol = max(4, (len(handles) + 3) // 4)
    ax.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.15),
        ncol=ncol,
        frameon=False,
        fontsize=7,
        columnspacing=1.0,
    )
    ax.set_title(
        "P99 latency vs cryptographic load $\\Sigma$\n(all caching configurations)", fontsize=14
    )
    _grid(ax)
    fig.subplots_adjust(bottom=0.32)
    _save(fig, output)


def plot_latency_corrected(summaries: list[RunSummary], output: Path):
    """Transport-corrected data-collapse plot.

    Model: P99_observed = f(Sigma) + t_tcp.
    Plot P99_corrected = P99 - t_tcp vs Sigma (pure cryptographic load).
    TCP algorithms are shifted down by the measured transport overhead,
    which should collapse the UDP and TCP clusters onto one curve.
    """
    from src.analyzer import TRANSPORT_OVERHEAD_MS

    _style()
    fig, ax = plt.subplots()

    by_algo: dict[str, list[RunSummary]] = {}
    for s in summaries:
        if s.sigma is not None and s.simulated_delay_ms is None:
            by_algo.setdefault(s.algorithm, []).append(s)

    for algo in _ALGO_ORDER:
        if algo not in by_algo:
            continue
        t_tcp = TRANSPORT_OVERHEAD_MS.get(algo, 0.0)
        groups = _group_by_sigma(by_algo[algo])
        sigmas = sorted(groups.keys())
        means = [np.mean(groups[s]) - t_tcp for s in sigmas]
        stds = [np.std(groups[s]) for s in sigmas]

        ax.errorbar(
            sigmas,
            means,
            yerr=stds,
            fmt="none",
            ecolor=ALGO_COLORS[algo],
            elinewidth=1.2,
            capsize=3,
            capthick=1.2,
        )
        ax.scatter(
            sigmas,
            means,
            color=ALGO_COLORS[algo],
            marker=ALGO_MARKERS[algo],
            edgecolor="black",
            linewidth=1.2,
            s=100,
            label=ALGO_LABELS.get(algo, algo),
            zorder=5,
        )

    # ── simulated-delay overlay (transport-corrected) ───────────────────
    sim_runs = [s for s in summaries if _valid_sim(s) and s.sigma is not None]
    sim_runs.sort(key=lambda s: (s.algorithm, s.simulated_delay_ms))
    for s in sim_runs:
        t_tcp = TRANSPORT_OVERHEAD_MS.get(s.algorithm, 0.0)
        is_tcp = t_tcp > 0
        y = s.latency_p99 - t_tcp
        ax.scatter(
            s.sigma,
            y,
            color=_SIM_COLOR,
            marker=_SIM_MARKER_TCP if is_tcp else _SIM_MARKER_UDP,
            s=_SIM_SIZE,
            edgecolor=_SIM_EDGE,
            linewidth=0.7,
            alpha=0.85,
            zorder=4,
        )
        ax.annotate(
            _sim_label(s),
            (s.sigma, y),
            textcoords="offset points",
            xytext=(6, -4),
            fontsize=6,
            color=_SIM_EDGE,
            fontweight="bold",
        )

    ax.set_xlabel(r"Cryptographic load $\Sigma$", labelpad=10)
    ax.set_ylabel(
        r"Transport-corrected P99 (ms)  " r"$[P_{99} - \bar{t}_{\mathrm{tcp}}]$",
        labelpad=20,
    )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.axvline(x=1, color="grey", linestyle=":", linewidth=1.5, alpha=0.7)
    ax.text(
        1.15,
        ax.get_ylim()[0] * 1.3 if ax.get_ylim()[0] > 0 else 3,
        r"$\Sigma = 1$",
        color="grey",
        fontsize=10,
        ha="left",
        va="bottom",
    )
    _draw_baseline(ax, _baseline_p99(summaries))

    from matplotlib.lines import Line2D

    handles, labels = ax.get_legend_handles_labels()
    if sim_runs:
        handles += [
            Line2D(
                [],
                [],
                marker=_SIM_MARKER_UDP,
                color=_SIM_COLOR,
                markeredgecolor=_SIM_EDGE,
                linestyle="none",
                markersize=6,
                label="Simulated (UDP)",
            ),
            Line2D(
                [],
                [],
                marker=_SIM_MARKER_TCP,
                color=_SIM_COLOR,
                markeredgecolor=_SIM_EDGE,
                linestyle="none",
                markersize=6,
                label="Simulated (TCP)",
            ),
        ]
    ax.legend(handles=handles, loc="upper left", framealpha=0.9)
    ax.set_title("Transport-corrected P99 vs $\\Sigma$\n(all caching configurations)", fontsize=14)
    _grid(ax)
    _save(fig, output)


def plot_regime_map(summaries: list[RunSummary], output: Path):
    """Regime map: mean P99 ± std per algorithm vs churn rate."""
    _style()
    fig, ax = plt.subplots()

    from matplotlib.lines import Line2D

    by_algo: dict[str, list[RunSummary]] = {}
    for s in summaries:
        if s.simulated_delay_ms is None:
            by_algo.setdefault(s.algorithm, []).append(s)

    algo_list = [a for a in _ALGO_ORDER if a in by_algo]
    n_algos = len(algo_list)
    jitter_range = 0.06

    for i, algo in enumerate(algo_list):
        runs = by_algo[algo]
        color = ALGO_COLORS[algo]
        marker = ALGO_MARKERS[algo]
        jitter = (i - n_algos / 2) * jitter_range

        groups = _group_by_churn(runs)
        crs = sorted(groups.keys())
        means = [np.mean(groups[c]) for c in crs]
        stds = [np.std(groups[c]) for c in crs]
        crs_j = [c * (10**jitter) for c in crs]

        ax.errorbar(
            crs_j,
            means,
            yerr=_log_safe_yerr(means, stds),
            fmt="none",
            ecolor=color,
            elinewidth=1.2,
            capsize=3,
            capthick=1.2,
        )
        ax.scatter(
            crs_j,
            means,
            color=color,
            marker=marker,
            edgecolor="black",
            linewidth=1.2,
            s=100,
            zorder=5,
        )

        if len(crs) > 1:
            ax.plot(crs_j, means, color=color, alpha=0.4, linewidth=1.2, linestyle="--")

    ax.set_xlabel("Service churn rate (svc/s)", labelpad=10)
    ax.set_ylabel("P99 latency (ms)", labelpad=20)
    ax.set_xscale("log")
    ax.set_yscale("log")
    _draw_baseline(ax, _baseline_p99(summaries))
    ax.set_title("Caching regime map")
    _grid(ax)

    handles = [
        Line2D(
            [0],
            [0],
            marker=ALGO_MARKERS[a],
            color=ALGO_COLORS[a],
            linestyle="--",
            markeredgecolor="black",
            markeredgewidth=1.2,
            markersize=8,
            linewidth=1,
            alpha=0.8,
            label=ALGO_LABELS.get(a, a),
        )
        for a in algo_list
    ]
    L0 = _baseline_p99(summaries)
    if L0 is not None:
        handles.append(
            Line2D(
                [0],
                [0],
                color="#d35400",
                linestyle="--",
                linewidth=1.3,
                alpha=0.7,
                label=f"$L_0$ baseline ({L0:.1f} ms)",
            )
        )
    ax.legend(handles=handles, loc="upper left", framealpha=0.9)
    _save(fig, output)


def plot_latency_distributions(summaries: list[RunSummary], output: Path):
    """Grouped bar chart: P50 / P95 / P99 per algorithm (mean ± std).

    Uses the no-cache regime (sc=0, ttl=0) to show worst-case latencies
    where every query goes through signing.
    """
    _style()
    fig, ax = plt.subplots()

    def _nocache(s: RunSummary) -> bool:
        return (s.sig_cache_cap or 0) == 0 and int(s.cache_ttl or 0) == 0

    def _no_netem(s: RunSummary) -> bool:
        return s.network_delay_ms in (None, 0) and s.network_rate_kbps is None

    real = [s for s in summaries if s.simulated_delay_ms is None and _nocache(s) and _no_netem(s)]
    sim = [s for s in summaries if _valid_sim(s) and _nocache(s) and _no_netem(s)]
    sim.sort(key=lambda s: (s.algorithm, s.simulated_delay_ms))

    # Build entries: (label, runs_list, is_sim)
    entries: list[tuple[str, list[RunSummary], bool]] = []
    for algo in _ALGO_ORDER:
        runs = [s for s in real if s.algorithm == algo]
        if runs:
            entries.append((ALGO_LABELS.get(algo, algo), runs, False))
    for s in sim:
        entries.append((_sim_label(s), [s], True))

    x = np.arange(len(entries))
    width = 0.25

    percentile_colors = list(_CMAP(np.linspace(0.1, 0.9, 3)))
    sim_pct_colors = ["#DAA520", "#C49000", "#8B6914"]  # light→dark goldenrod

    for i, percentile in enumerate(["latency_p50", "latency_p95", "latency_p99"]):
        means, stds, bar_colors = [], [], []
        for lbl, runs, is_sim in entries:
            vals = [getattr(s, percentile) for s in runs]
            means.append(np.mean(vals) if vals else 0)
            stds.append(np.std(vals) if vals else 0)
            bar_colors.append(sim_pct_colors[i] if is_sim else percentile_colors[i])
        label = percentile.replace("latency_", "").upper()
        ax.bar(
            x + i * width,
            means,
            width,
            yerr=_log_safe_yerr(means, stds),
            label=label,
            color=bar_colors,
            edgecolor="black",
            linewidth=1.2,
            capsize=3,
            error_kw={"elinewidth": 1.2, "capthick": 1.2},
        )

    ax.set_xticks(x + width)
    ax.set_xticklabels([e[0] for e in entries], rotation=40, ha="right", fontsize=9)
    ax.set_ylabel("Latency (ms)", labelpad=20)
    ax.set_yscale("log")
    ax.legend()
    ax.set_title("Latency distribution (no-cache regime)")
    _grid(ax)
    _save(fig, output)


def plot_signing_regime(summaries: list[RunSummary], output: Path):
    """Pure signing regime: P99 vs Σ for sc=0/TTL=0 runs only.

    Every query triggers a fresh signing operation (no caching).
    Σ = query_rate × s̄, giving the cleanest view of the
    cryptographic-load → latency relationship.
    Shaded convex-hull clusters separate UDP and TCP algorithms.
    """
    from scipy.spatial import ConvexHull

    _style()
    fig, ax = plt.subplots()

    no_cache = [
        s
        for s in summaries
        if s.sig_cache_cap == 0
        and (s.cache_ttl is not None and s.cache_ttl == 0)
        and s.sigma is not None
    ]

    from src.analyzer import TRANSPORT_OVERHEAD_MS

    by_algo: dict[str, list[RunSummary]] = {}
    for s in no_cache:
        if s.simulated_delay_ms is None:
            by_algo.setdefault(s.algorithm, []).append(s)

    # Collect points for TCP/UDP clusters (log-space)
    udp_pts, tcp_pts = [], []

    for algo in _ALGO_ORDER:
        if algo not in by_algo:
            continue
        is_tcp = TRANSPORT_OVERHEAD_MS.get(algo, 0) > 0
        runs = sorted(by_algo[algo], key=lambda r: r.sigma)
        sigmas = [r.sigma for r in runs]
        p99s = [r.latency_p99 for r in runs]

        ax.scatter(
            sigmas,
            p99s,
            color=ALGO_COLORS[algo],
            marker=ALGO_MARKERS[algo],
            edgecolor="black",
            linewidth=1.2,
            s=120,
            label=ALGO_LABELS.get(algo, algo),
            zorder=5,
            alpha=0.8,
        )
        if len(sigmas) > 1:
            ax.plot(
                sigmas, p99s, color=ALGO_COLORS[algo], alpha=0.4, linewidth=1.2, linestyle="--"
            )

        bucket = tcp_pts if is_tcp else udp_pts
        for sig, p99 in zip(sigmas, p99s):
            bucket.append((np.log10(sig), np.log10(p99)))

    ax.set_xlabel(r"Cryptographic load $\Sigma = \lambda_q \cdot \bar{s}$", labelpad=10)
    ax.set_ylabel("P99 latency (ms)", labelpad=20)
    ax.set_xscale("log")
    ax.set_yscale("log")

    # ── simulated-delay overlay ─────────────────────────────────────────
    sim_no_cache = [
        s
        for s in summaries
        if _valid_sim(s)
        and s.sig_cache_cap == 0
        and (s.cache_ttl is not None and s.cache_ttl == 0)
        and s.sigma is not None
    ]
    sim_no_cache.sort(key=lambda s: (s.algorithm, s.simulated_delay_ms))
    for s in sim_no_cache:
        is_tcp = TRANSPORT_OVERHEAD_MS.get(s.algorithm, 0) > 0
        ax.scatter(
            s.sigma,
            s.latency_p99,
            color=_SIM_COLOR,
            marker=_SIM_MARKER_TCP if is_tcp else _SIM_MARKER_UDP,
            s=_SIM_SIZE,
            edgecolor=_SIM_EDGE,
            linewidth=0.7,
            alpha=0.85,
            zorder=4,
        )
        ax.annotate(
            _sim_label(s),
            (s.sigma, s.latency_p99),
            textcoords="offset points",
            xytext=(6, -4),
            fontsize=6,
            color=_SIM_EDGE,
            fontweight="bold",
        )
        bucket = tcp_pts if is_tcp else udp_pts
        bucket.append((np.log10(s.sigma), np.log10(s.latency_p99)))

    # ── shaded convex-hull clusters ─────────────────────────────────────
    def _draw_cluster(pts_log, color, label):
        if len(pts_log) < 3:
            return
        pts = np.array(pts_log)
        # Pad slightly so hull doesn't clip markers
        centroid = pts.mean(axis=0)
        padded = centroid + (pts - centroid) * 1.15
        try:
            hull = ConvexHull(padded)
        except Exception:
            return
        verts_log = padded[hull.vertices]
        verts_log = np.vstack([verts_log, verts_log[0]])  # close polygon
        verts_x = 10 ** verts_log[:, 0]
        verts_y = 10 ** verts_log[:, 1]
        ax.fill(verts_x, verts_y, color=color, alpha=0.10, zorder=1)
        ax.plot(verts_x, verts_y, color=color, alpha=0.35, linewidth=1.5, linestyle="--", zorder=1)
        # Label at top-right vertex
        idx = np.argmax(verts_log[:-1, 0] + verts_log[:-1, 1])
        ax.annotate(
            label,
            (verts_x[idx], verts_y[idx]),
            textcoords="offset points",
            xytext=(8, 6),
            fontsize=9,
            fontweight="bold",
            color=color,
            alpha=0.7,
        )

    _draw_cluster(udp_pts, "#2ca02c", "UDP")
    _draw_cluster(tcp_pts, "#d62728", "TCP")

    ax.axvline(x=1, color="grey", linestyle=":", linewidth=1.5, alpha=0.7)
    ax.text(
        1.15,
        ax.get_ylim()[0] * 1.3 if ax.get_ylim()[0] > 0 else 3,
        r"$\Sigma = 1$",
        color="grey",
        fontsize=10,
        ha="left",
        va="bottom",
    )
    _draw_baseline(ax, _baseline_p99(summaries))

    from matplotlib.lines import Line2D

    handles, labels = ax.get_legend_handles_labels()
    if sim_no_cache:
        handles += [
            Line2D(
                [],
                [],
                marker=_SIM_MARKER_UDP,
                color=_SIM_COLOR,
                markeredgecolor=_SIM_EDGE,
                linestyle="none",
                markersize=6,
                label="Simulated (UDP)",
            ),
            Line2D(
                [],
                [],
                marker=_SIM_MARKER_TCP,
                color=_SIM_COLOR,
                markeredgecolor=_SIM_EDGE,
                linestyle="none",
                markersize=6,
                label="Simulated (TCP)",
            ),
        ]
    # Cluster legend entries
    from matplotlib.patches import Patch

    handles += [
        Patch(
            facecolor="#2ca02c",
            alpha=0.15,
            edgecolor="#2ca02c",
            linewidth=1.5,
            linestyle="--",
            label="UDP cluster",
        ),
        Patch(
            facecolor="#d62728",
            alpha=0.15,
            edgecolor="#d62728",
            linewidth=1.5,
            linestyle="--",
            label="TCP cluster",
        ),
    ]
    ncol = max(3, (len(handles) + 2) // 3)
    ax.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.18),
        ncol=ncol,
        frameon=False,
        fontsize=9,
    )
    ax.set_title("Signing regime identification (no-cache regime)")
    _grid(ax)
    fig.subplots_adjust(bottom=0.28)
    _save(fig, output)


def plot_caching_benefit(summaries: list[RunSummary], output: Path):
    """Caching benefit: P99 per algorithm across caching regimes at peak load.

    Shows grouped bars for each algorithm at (cr=5, qr=500), one bar per
    caching regime.  Reveals when caching helps (slow PQC) vs hurts
    (fast algorithms where cache-plugin overhead exceeds signing cost).
    """
    _style()

    # Select highest-load runs (exclude simulated & netem-only)
    high = [
        s
        for s in summaries
        if s.simulated_delay_ms is None
        and s.network_delay_ms in (None, 0)
        and s.network_rate_kbps is None
    ]
    if not high:
        log.warning("No non-emulated runs for caching_benefit – skipping")
        return
    # Pick the highest (cr, qr) combination present in the data
    _peak = max(high, key=lambda s: (s.churn_rate, s.query_rate))
    high = [
        s for s in high if s.churn_rate == _peak.churn_rate and s.query_rate == _peak.query_rate
    ]

    # Detect available regimes from data
    available = {((s.sig_cache_cap or 0), int(s.cache_ttl or 0)) for s in high}
    all_regimes = [
        (0, 0, "No cache"),
        (0, 5, "TTL=5"),
        (0, 30, "TTL=30"),
        (10000, 0, "Sig cache"),
        (10000, 5, "Both (TTL=5)"),
        (10000, 30, "Both (TTL=30)"),
    ]
    regimes = [(sc, ttl, lbl) for sc, ttl, lbl in all_regimes if (sc, ttl) in available]
    if len(regimes) < 1:
        log.warning("No caching regimes matched – skipping caching_benefit")
        return

    algos = [a for a in _ALGO_ORDER if any(s.algorithm == a for s in high)]

    fig, ax = plt.subplots(figsize=(13, 6))
    n_regimes = len(regimes)
    width = 0.8 / max(n_regimes, 1)
    x = np.arange(len(algos))

    regime_colors = list(_CMAP(np.linspace(0.0, 1.0, n_regimes)))

    for i, (sc, ttl, label) in enumerate(regimes):
        vals = []
        for algo in algos:
            matches = [
                s.latency_p99
                for s in high
                if s.algorithm == algo
                and (s.sig_cache_cap or 0) == sc
                and int(s.cache_ttl or 0) == ttl
            ]
            vals.append(matches[0] if matches else 0)
        ax.bar(
            x + i * width,
            vals,
            width,
            label=label,
            color=regime_colors[i],
            edgecolor="black",
            linewidth=0.8,
        )

    ax.set_yscale("log")
    ax.set_xticks(x + width * (n_regimes - 1) / 2)
    ax.set_xticklabels([ALGO_LABELS.get(a, a) for a in algos], rotation=30, ha="right")
    ax.set_ylabel("P99 latency (ms)", labelpad=20)
    _draw_baseline(ax, _baseline_p99(summaries))
    ax.legend(title="Caching regime", loc="upper left", framealpha=0.9)
    ax.set_title("Caching benefit (peak load)")
    _grid(ax)
    _save(fig, output)


def plot_caching_ratio(summaries: list[RunSummary], campaign_dir: Path, output: Path):
    """Caching speedup R = P99(no-cache)/P99(best-cache) with bootstrap CIs.

    Lollipop chart at peak load (cr=5, qr=500), algorithms sorted by
    signing cost s̄.  The non-monotonic relationship (R rises with s̄
    then drops for extreme costs) reveals when caching is most effective.
    """
    _style()

    high = [
        s
        for s in summaries
        if s.simulated_delay_ms is None
        and s.network_delay_ms in (None, 0)
        and s.network_rate_kbps is None
    ]
    if not high:
        log.warning("No non-emulated runs for caching_ratio – skipping")
        return
    _peak = max(high, key=lambda s: (s.churn_rate, s.query_rate))
    high = [
        s for s in high if s.churn_rate == _peak.churn_rate and s.query_rate == _peak.query_rate
    ]
    algos = sorted(
        {s.algorithm for s in high},
        key=lambda a: SIGNING_TIMES_MS.get(a, 0),
    )

    rng = np.random.default_rng(42)
    n_boot = 2000
    rows: list[tuple[str, float, float, float, str, float]] = []

    for algo in algos:
        nocache = [
            s
            for s in high
            if s.algorithm == algo and (s.sig_cache_cap or 0) == 0 and int(s.cache_ttl or 0) == 0
        ]
        cached = [
            s
            for s in high
            if s.algorithm == algo
            and not ((s.sig_cache_cap or 0) == 0 and int(s.cache_ttl or 0) == 0)
        ]
        if not nocache or not cached:
            continue

        nc = nocache[0]
        best = min(cached, key=lambda s: s.latency_p99)

        # Load raw latencies and bootstrap the ratio
        lats_nc = _load_raw_latencies(campaign_dir, nc)
        lats_best = _load_raw_latencies(campaign_dir, best)
        n_nc, n_best = len(lats_nc), len(lats_best)

        R_boots = np.empty(n_boot)
        for i in range(n_boot):
            p99_nc = np.percentile(rng.choice(lats_nc, size=n_nc, replace=True), 99)
            p99_b = np.percentile(rng.choice(lats_best, size=n_best, replace=True), 99)
            R_boots[i] = p99_nc / p99_b

        R = nc.latency_p99 / best.latency_p99
        R_lo = float(np.percentile(R_boots, 2.5))
        R_hi = float(np.percentile(R_boots, 97.5))

        sc = best.sig_cache_cap or 0
        ttl = int(best.cache_ttl or 0)
        regime = (
            f"Sig+TTL={ttl}" if sc > 0 and ttl > 0 else "Sig cache" if sc > 0 else f"TTL={ttl}"
        )
        s_bar = SIGNING_TIMES_MS.get(algo, 0)
        rows.append((algo, R, R_lo, R_hi, regime, s_bar))

    # ── figure ──────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(11, 7))

    y = np.arange(len(rows))
    Rs = np.array([r[1] for r in rows])
    R_los = np.array([r[2] for r in rows])
    R_his = np.array([r[3] for r in rows])

    # Lollipop stems from R=1 to R
    for i, (algo, R, _, _, _, _) in enumerate(rows):
        ax.plot(
            [1, R],
            [i, i],
            color=ALGO_COLORS.get(algo, "#888888"),
            linewidth=2.5,
            solid_capstyle="round",
            zorder=3,
        )

    # Error bars (clamp to avoid negative xerr from bootstrap noise)
    xerr_lo = np.maximum(Rs - R_los, 0)
    xerr_hi = np.maximum(R_his - Rs, 0)
    ax.errorbar(
        Rs,
        y,
        xerr=[xerr_lo, xerr_hi],
        fmt="none",
        ecolor="grey",
        capsize=4,
        linewidth=1.2,
        zorder=4,
    )

    # Dots
    for i, (algo, R, _, _, _, _) in enumerate(rows):
        ax.plot(
            R,
            i,
            "o",
            color=ALGO_COLORS.get(algo, "#888888"),
            markersize=11,
            markeredgecolor="black",
            markeredgewidth=0.8,
            zorder=5,
        )

    # Annotate R value and winning regime
    for i, (_, R, _, R_hi_val, regime, _) in enumerate(rows):
        ax.text(R_his[i] * 1.12, i, f" {R:.1f}$\\times$ ({regime})", va="center", fontsize=9)

    # y-tick labels: algorithm + signing cost
    labels = [f"{ALGO_LABELS.get(a, a)}  ($\\bar{{s}}$ = {s:.4g} ms)" for a, _, _, _, _, s in rows]
    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel(
        "Caching Speedup  "
        "$R = P_{99}^{\\mathrm{no\\;cache}}"
        " \\,/\\, P_{99}^{\\mathrm{best\\;cache}}$"
    )
    ax.set_xscale("log")
    ax.axvline(
        1, color="red", linestyle="--", linewidth=1, alpha=0.7, label="$R = 1$ (no benefit)"
    )
    ax.legend(loc="lower right", framealpha=0.9)
    ax.set_title("Caching speedup ratio (peak load)")
    _grid(ax)
    fig.subplots_adjust(left=0.30)
    _save(fig, output)


def plot_caching_theory(summaries: list[RunSummary], campaign_dir: Path, output: Path):
    """Caching speedup R vs signing cost s̄ with M/G/1 lower-bound curve.

    Compares experimental R = P99(no-cache) / P99(best-cache) at peak load
    (cr=5, qr=500) against the M/G/1 theoretical prediction.  The caching
    window [1/qr, 1/cr] is shown as a zero-free-parameter prediction from
    the Sigma model.
    """
    _style()

    high = [
        s
        for s in summaries
        if s.simulated_delay_ms is None
        and s.network_delay_ms in (None, 0)
        and s.network_rate_kbps is None
    ]
    if not high:
        log.warning("No non-emulated runs for caching_theory – skipping")
        return
    _peak = max(high, key=lambda s: (s.churn_rate, s.query_rate))
    high = [
        s for s in high if s.churn_rate == _peak.churn_rate and s.query_rate == _peak.query_rate
    ]
    qr, cr = int(_peak.query_rate), int(_peak.churn_rate)
    algos = sorted(
        {s.algorithm for s in high},
        key=lambda a: SIGNING_TIMES_MS.get(a, 0),
    )

    # ── experimental R with bootstrap CIs ───────────────────────────────
    rng = np.random.default_rng(42)
    n_boot = 2000
    exp_s, exp_R, exp_R_lo, exp_R_hi, exp_algo = [], [], [], [], []

    for algo in algos:
        nocache = [
            s
            for s in high
            if s.algorithm == algo and (s.sig_cache_cap or 0) == 0 and int(s.cache_ttl or 0) == 0
        ]
        cached = [
            s
            for s in high
            if s.algorithm == algo
            and not ((s.sig_cache_cap or 0) == 0 and int(s.cache_ttl or 0) == 0)
        ]
        if not nocache or not cached:
            continue
        nc = nocache[0]
        best = min(cached, key=lambda s: s.latency_p99)

        lats_nc = _load_raw_latencies(campaign_dir, nc)
        lats_best = _load_raw_latencies(campaign_dir, best)
        n_nc, n_b = len(lats_nc), len(lats_best)
        R_boots = np.array(
            [
                np.percentile(rng.choice(lats_nc, n_nc, replace=True), 99)
                / np.percentile(rng.choice(lats_best, n_b, replace=True), 99)
                for _ in range(n_boot)
            ]
        )

        s_bar = SIGNING_TIMES_MS.get(algo, 0)
        R = nc.latency_p99 / best.latency_p99
        exp_s.append(s_bar)
        exp_R.append(R)
        exp_R_lo.append(float(np.percentile(R_boots, 2.5)))
        exp_R_hi.append(float(np.percentile(R_boots, 97.5)))
        exp_algo.append(algo)

    exp_s = np.array(exp_s)
    exp_R = np.array(exp_R)
    exp_R_lo = np.array(exp_R_lo)
    exp_R_hi = np.array(exp_R_hi)

    # ── M/G/1 theoretical curve (lower bound) ──────────────────────────
    # Use measured unsigned baseline L0 when available; else heuristic
    L0_measured = _baseline_p99(summaries)
    if L0_measured is not None:
        L0 = L0_measured
    else:
        cached_base = [
            s.latency_p99
            for s in high
            if (s.sig_cache_cap or 0) > 0
            and int(s.cache_ttl or 0) > 0
            and SIGNING_TIMES_MS.get(s.algorithm, 0) < 0.5
        ]
        L0 = float(np.median(cached_base)) if cached_base else 4.0

    s_range = np.logspace(np.log10(0.01), np.log10(300), 500)

    def _p99_mg1(s_bar_ms, qr_eff, L0_val):
        """M/G/1 P99 of response time (deterministic service, C_s≈0)."""
        sigma = qr_eff * s_bar_ms / 1000
        if sigma >= 1:
            return 5000.0  # server-saturated cap
        # Pollaczek-Khinchine + exponential tail for P99 of waiting time
        E_W = sigma * s_bar_ms / (2 * (1 - sigma))
        return L0_val + s_bar_ms + 4.605 * E_W

    R_mg1 = np.array([_p99_mg1(s, qr, L0) / _p99_mg1(s, cr, L0) for s in s_range])

    # ── caching window bounds ───────────────────────────────────────────
    s_nc1 = 1000 / qr  # Sigma_nc = 1
    s_c1 = 1000 / cr  # Sigma_c = 1

    # ── figure ──────────────────────────────────────────────────────────
    fig, ax = plt.subplots(figsize=(12, 7))

    # Shaded caching window
    ax.axvspan(
        s_nc1,
        s_c1,
        alpha=0.10,
        color="#33acdc",
        label=f"Caching window " f"$1/q_r < \\bar{{s}} < 1/c_r$",
    )

    # Window bound lines
    ax.axvline(s_nc1, color="#33acdc", linestyle="--", linewidth=1.2, alpha=0.7)
    ax.axvline(s_c1, color="#33acdc", linestyle="--", linewidth=1.2, alpha=0.7)

    # M/G/1 theoretical curve
    ax.plot(
        s_range,
        R_mg1,
        color="grey",
        linewidth=2,
        linestyle="-",
        alpha=0.6,
        label="M/G/1 lower bound",
        zorder=2,
    )

    # R = 1 reference
    ax.axhline(1, color="red", linestyle=":", linewidth=1, alpha=0.5)

    # Experimental points
    for i, algo in enumerate(exp_algo):
        ax.errorbar(
            exp_s[i],
            exp_R[i],
            yerr=[[exp_R[i] - exp_R_lo[i]], [exp_R_hi[i] - exp_R[i]]],
            fmt="o",
            color=ALGO_COLORS.get(algo, "#888888"),
            markersize=10,
            markeredgecolor="black",
            markeredgewidth=0.8,
            ecolor="black",
            capsize=4,
            linewidth=1.2,
            zorder=5,
        )
        # Label each point
        label = ALGO_LABELS.get(algo, algo)
        x_off = 1.15 if exp_R[i] > 3 else 1.25
        y_off = 1.0
        ha = "left"
        # Special positioning to avoid overlap
        if exp_R[i] < 2 and exp_s[i] < 0.3:
            y_off = 1.15  # push up slightly for the cluster
        ax.annotate(
            label,
            (exp_s[i], exp_R[i]),
            textcoords="offset points",
            xytext=(8, 4),
            fontsize=8,
            ha=ha,
            va="bottom",
        )

    # Axis labels at window bounds
    ax.text(
        s_nc1,
        0.72,
        "$\\Sigma_{nc}=1$",
        transform=ax.get_xaxis_transform(),
        ha="center",
        fontsize=9,
        color="#2090b0",
        fontstyle="italic",
    )
    ax.text(
        s_c1,
        0.72,
        "$\\Sigma_{c}=1$",
        transform=ax.get_xaxis_transform(),
        ha="center",
        fontsize=9,
        color="#2090b0",
        fontstyle="italic",
    )

    # ── Simulated runs: R_sim = P99(sim,nc) / P99(base_algo, best-cache) ─
    # Lookup the real best-cache P99 for each base algorithm, then plot
    # simulated points at (s_eff, R_sim) to show how caching benefit
    # grows with signing cost — validated by controlled-delay experiments.
    best_cache_p99: dict[str, float] = {}
    for algo in algos:
        cached = [
            s
            for s in high
            if s.algorithm == algo
            and not ((s.sig_cache_cap or 0) == 0 and int(s.cache_ttl or 0) == 0)
        ]
        if cached:
            best_cache_p99[algo] = min(s.latency_p99 for s in cached)

    sim_runs = [s for s in summaries if _valid_sim(s)]
    # Group by base algorithm and sort by delay
    sim_by_algo: dict[str, list] = {}
    for s in sim_runs:
        if s.algorithm in best_cache_p99:
            sim_by_algo.setdefault(s.algorithm, []).append(s)

    from src.analyzer import TRANSPORT_OVERHEAD_MS
    from matplotlib.lines import Line2D

    for algo, runs in sim_by_algo.items():
        runs_sorted = sorted(runs, key=lambda r: r.simulated_delay_ms)
        xs, ys = [], []
        for r in runs_sorted:
            s_eff = r.simulated_delay_ms + SIGNING_TIMES_MS.get(algo, 0)
            R_sim = r.latency_p99 / best_cache_p99[algo]
            xs.append(s_eff)
            ys.append(R_sim)
        # Trajectory line
        ax.plot(xs, ys, color=_SIM_COLOR, linewidth=1.2, linestyle=":", alpha=0.6, zorder=3)
        # Individual markers
        is_tcp = TRANSPORT_OVERHEAD_MS.get(algo, 0) > 0
        marker = _SIM_MARKER_TCP if is_tcp else _SIM_MARKER_UDP
        ax.scatter(
            xs,
            ys,
            color=_SIM_COLOR,
            marker=marker,
            s=_SIM_SIZE,
            edgecolor=_SIM_EDGE,
            linewidth=0.7,
            zorder=6,
        )
        # Label each point with its sim_label
        for r, xi, yi in zip(runs_sorted, xs, ys):
            ax.annotate(
                _sim_label(r),
                (xi, yi),
                textcoords="offset points",
                xytext=(8, -4),
                fontsize=6,
                ha="left",
                va="top",
                color=_SIM_EDGE,
                fontweight="bold",
            )

    # Add simulated legend entries if any were plotted
    if sim_by_algo:
        handles, labels = ax.get_legend_handles_labels()
        handles.append(
            Line2D(
                [],
                [],
                marker=_SIM_MARKER_UDP,
                color=_SIM_COLOR,
                markeredgecolor=_SIM_EDGE,
                linestyle=":",
                linewidth=1.2,
                markersize=6,
                label="Simulated (UDP)",
            )
        )
        labels.append("Simulated (UDP)")
        if any(TRANSPORT_OVERHEAD_MS.get(a, 0) > 0 for a in sim_by_algo):
            handles.append(
                Line2D(
                    [],
                    [],
                    marker=_SIM_MARKER_TCP,
                    color=_SIM_COLOR,
                    markeredgecolor=_SIM_EDGE,
                    linestyle=":",
                    linewidth=1.2,
                    markersize=6,
                    label="Simulated (TCP)",
                )
            )
            labels.append("Simulated (TCP)")
        ax.legend(handles=handles, labels=labels, loc="upper left", framealpha=0.9, fontsize=10)

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Signing cost $\\bar{s}$ (ms)")
    ax.set_ylabel(
        "Caching speedup  "
        "$R = P_{99}^{\\mathrm{no\\;cache}}"
        " \\,/\\, P_{99}^{\\mathrm{best\\;cache}}$"
    )
    ax.legend(loc="upper left", framealpha=0.9, fontsize=10)
    ax.set_title("M/G/1 caching theory vs measured")
    _grid(ax)
    _save(fig, output)


# ── PE-WASUN-equivalent and new K8s plots ──────────────────────────────


def plot_signing_times(output: Path):
    """Bar chart of microbenchmark signing times (PE-WASUN Fig 1 equivalent).

    Reads signing_bench.json; standalone (does not need RunSummary data).
    """
    _style()
    bench_path = Path(__file__).resolve().parent.parent / "results" / "signing_bench.json"
    if not bench_path.exists():
        log.warning("signing_bench.json not found – skipping signing_times plot")
        return
    with open(bench_path) as f:
        bench = json.load(f)

    algos = [a for a in _ALGO_ORDER if a in bench]
    means = [bench[a]["mean_ms"] for a in algos]
    stds = [bench[a].get("std_ms", 0) for a in algos]

    fig, ax = plt.subplots()
    x = np.arange(len(algos))
    colors = [ALGO_COLORS.get(a, "grey") for a in algos]
    ax.bar(
        x,
        means,
        yerr=_log_safe_yerr(means, stds),
        color=colors,
        edgecolor="black",
        linewidth=1.2,
        capsize=4,
        error_kw={"elinewidth": 1.2, "capthick": 1.2},
    )

    ax.set_xticks(x)
    ax.set_xticklabels([ALGO_LABELS.get(a, a) for a in algos], rotation=30, ha="right")
    ax.set_ylabel("Signing time (ms)", labelpad=20)
    ax.set_yscale("log")
    ax.set_title("Signing microbenchmark")
    _grid(ax)
    _save(fig, output)


def plot_response_sizes(summaries: list[RunSummary], output: Path):
    """Paired bar chart: unsigned vs signed DNSSEC response per algorithm.

    Two bars per algorithm: unsigned baseline (hatched) and signed (solid).
    Falcon variants get error bars (only PQC scheme with variable-length
    signatures due to discrete-Gaussian compression).  Log y-axis so
    SLH-DSA doesn't dwarf the rest.
    Includes simulated-delay runs (which share the same key/sig sizes as
    their base algorithm, confirming response size is delay-independent).
    """
    _style()
    real = [
        s
        for s in summaries
        if s.simulated_delay_ms is None
        and s.response_size_max is not None
        and s.response_size_max > 0
    ]
    if not real:
        log.warning("No response_size data – skipping response_sizes plot")
        return

    # Build entries: real algorithms only (simulated share the same key sizes)
    entries: list[tuple[str, list[RunSummary], bool, str]] = []
    for algo in _ALGO_ORDER:
        runs = [s for s in real if s.algorithm == algo]
        if runs:
            entries.append((ALGO_LABELS.get(algo, algo), runs, False, algo))

    fig, ax = plt.subplots()
    x = np.arange(len(entries))
    slot = 0.38
    bar_w = slot - 0.02
    gap = (slot - bar_w) / 2

    signed_means, unsigned_sizes, signed_lo, signed_hi = [], [], [], []
    colors = []
    for lbl, runs, is_sim, base_algo in entries:
        maxes = [s.response_size_max for s in runs]
        mean_signed = np.mean(maxes)
        signed_means.append(mean_signed)
        mins = [s.response_size_min for s in runs if s.response_size_min is not None]
        unsigned_sizes.append(np.median(mins) if mins else 0)
        lo = mean_signed - min(maxes)
        hi = max(maxes) - mean_signed
        has_spread = (max(maxes) - min(maxes)) > 0.5
        signed_lo.append(lo if has_spread else 0)
        signed_hi.append(hi if has_spread else 0)
        colors.append(_SIM_COLOR if is_sim else ALGO_COLORS.get(base_algo, "grey"))

    ax.set_yscale("log")

    ax.axhline(
        1232,
        color="#d35400",
        linestyle="--",
        linewidth=1.3,
        alpha=0.7,
        zorder=1,
        label="EDNS(0) UDP limit (1232 B)",
    )

    bars_u = ax.bar(
        x - slot / 2 - gap,
        unsigned_sizes,
        bar_w,
        color=colors,
        edgecolor="black",
        linewidth=0.8,
        hatch="//",
        alpha=0.6,
        zorder=2,
        label="Unsigned response",
    )

    bars_s = ax.bar(
        x + slot / 2 + gap,
        signed_means,
        bar_w,
        yerr=[signed_lo, signed_hi],
        color=colors,
        edgecolor="black",
        linewidth=0.8,
        capsize=4,
        error_kw={"elinewidth": 1.5, "capthick": 1.5, "ecolor": "black"},
        zorder=2,
        label="Signed response (RRSIG)",
    )

    for patch in list(bars_u) + list(bars_s):
        patch.set_antialiased(False)

    for i, (sz, entry) in enumerate(zip(signed_means, entries)):
        if sz > 1232:
            ax.text(
                i + slot / 2 + gap,
                sz * 1.12,
                "TCP",
                ha="center",
                va="bottom",
                fontsize=8,
                fontweight="bold",
                color="#d35400",
            )

    ax.set_xticks(x)
    ax.set_xticklabels([e[0] for e in entries], rotation=40, ha="right", fontsize=9)
    ax.set_ylabel("Response size (bytes)", fontweight="bold", labelpad=20)
    leg = ax.legend(
        loc="upper center", bbox_to_anchor=(0.5, -0.28), ncol=3, frameon=False, fontsize=10
    )
    for t in leg.get_texts():
        t.set_fontweight("bold")
    ax.set_title("Signed response sizes")
    _grid(ax)
    _save(fig, output)


def plot_transport_breakdown(summaries: list[RunSummary], output: Path):
    """Two-panel stacked bar: unsigned-UDP / unsigned-TCP / signed-UDP / signed-TCP.

    Panel (a) no-cache (sc=0, ttl=0) and panel (b) sig-cache (sc=10000, ttl=0).
    """
    _style()

    # ── regime definitions ──────────────────────────────────────────────
    regimes = [
        ("No cache", lambda s: (s.sig_cache_cap or 0) == 0 and int(s.cache_ttl or 0) == 0),
        ("Signature cache", lambda s: (s.sig_cache_cap or 0) > 0),
    ]

    def _has_transport(s: RunSummary) -> bool:
        return (
            s.tcp_fraction is not None
            and s.unsigned_fraction is not None
            and s.response_size_min is not None
            and s.response_size_max is not None
        )

    def _base_filter(s: RunSummary) -> bool:
        return (
            s.simulated_delay_ms is None
            and s.network_delay_ms in (None, 0)
            and s.network_rate_kbps is None
            and _has_transport(s)
        )

    # Only keep regimes that have data
    panels = []
    for title, pred in regimes:
        runs = [s for s in summaries if _base_filter(s) and pred(s)]
        if runs:
            panels.append((title, runs))

    if not panels:
        log.warning("No transport data – skipping transport_breakdown plot")
        return

    ncols = len(panels)
    fig, axes = plt.subplots(1, ncols, figsize=(6 * ncols, 6), sharey=True)
    if ncols == 1:
        axes = [axes]

    for col, (panel_title, panel_runs) in enumerate(panels):
        ax = axes[col]

        # Build bar entries: real algos only (no simulated in this plot)
        entries: list[tuple[str, str, list[RunSummary]]] = []
        for algo in _ALGO_ORDER:
            runs = [s for s in panel_runs if s.algorithm == algo]
            if runs:
                entries.append((algo, ALGO_LABELS.get(algo, algo), runs))

        x = np.arange(len(entries))
        uns_udp, uns_tcp, sig_udp, sig_tcp = [], [], [], []
        bar_colors = []

        for key, label, runs in entries:
            uf = np.mean([s.unsigned_fraction for s in runs])
            sf = 1.0 - uf
            unsigned_size = runs[0].response_size_min
            signed_size = runs[0].response_size_max
            if unsigned_size <= 1232:
                uns_udp.append(uf)
                uns_tcp.append(0.0)
            else:
                uns_udp.append(0.0)
                uns_tcp.append(uf)
            if signed_size <= 1232:
                sig_udp.append(sf)
                sig_tcp.append(0.0)
            else:
                sig_udp.append(0.0)
                sig_tcp.append(sf)
            bar_colors.append(ALGO_COLORS.get(runs[0].algorithm, "grey"))

        b0 = [0.0] * len(entries)
        bars_uu = ax.bar(
            x,
            uns_udp,
            bottom=b0,
            color=bar_colors,
            edgecolor="black",
            linewidth=0.8,
            alpha=0.35,
            label="Unsigned (UDP)",
        )
        b1 = [a + b for a, b in zip(b0, uns_udp)]
        bars_su = ax.bar(
            x,
            sig_udp,
            bottom=b1,
            color=bar_colors,
            edgecolor="black",
            linewidth=0.8,
            label="Signed (UDP)",
        )
        b2 = [a + b for a, b in zip(b1, sig_udp)]
        bars_ut = ax.bar(
            x,
            uns_tcp,
            bottom=b2,
            color=bar_colors,
            edgecolor="black",
            linewidth=0.8,
            hatch="//",
            alpha=0.35,
            label="Unsigned (TCP)",
        )
        b3 = [a + b for a, b in zip(b2, uns_tcp)]
        bars_st = ax.bar(
            x,
            sig_tcp,
            bottom=b3,
            color=bar_colors,
            edgecolor="black",
            linewidth=0.8,
            hatch="//",
            label="Signed (TCP)",
        )

        for patch in list(bars_uu) + list(bars_su) + list(bars_ut) + list(bars_st):
            patch.set_antialiased(False)

        ax.set_xticks(x)
        labels = [e[1] for e in entries]
        ax.set_xticklabels(labels, rotation=40, ha="right", fontsize=9)
        ax.set_ylim(0, 1.05)
        letter = chr(ord("a") + col)
        ax.set_title(f"({letter}) {panel_title}", fontsize=11, fontweight="bold")
        _grid(ax)

    axes[0].set_ylabel("Query fraction", fontweight="bold", labelpad=20)

    # Shared legend from last axis
    handles, lbls = axes[-1].get_legend_handles_labels()
    fig.legend(
        handles,
        lbls,
        loc="lower center",
        bbox_to_anchor=(0.5, -0.02),
        ncol=4,
        frameon=False,
        fontsize=10,
    )
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.18)
    _save(fig, output)


def plot_throughput_vs_size(summaries: list[RunSummary], output: Path):
    """Scatter: achieved QPS vs signed response size (bytes), two panels.

    Left panel: baseline (local, no netem).
    Right panel: bandwidth-limited (500 Kbps).
    Reveals whether signature compactness correlates with throughput.
    Filtered to no-cache regime.
    """
    from src.analyzer import TRANSPORT_OVERHEAD_MS

    _style()

    def _nocache(s: RunSummary) -> bool:
        return (s.sig_cache_cap or 0) == 0 and int(s.cache_ttl or 0) == 0

    def _baseline(s: RunSummary) -> bool:
        return s.network_delay_ms in (None, 0) and s.network_rate_kbps is None

    def _bw_limited(s: RunSummary) -> bool:
        return s.network_rate_kbps is not None and s.network_rate_kbps > 0

    conditions: list[tuple[str, callable]] = [
        ("Local (no delay)", _baseline),
        ("Bandwidth limited", _bw_limited),
    ]

    real_base = [
        s
        for s in summaries
        if s.simulated_delay_ms is None
        and _nocache(s)
        and s.actual_qps is not None
        and s.actual_qps > 0
        and s.response_size_max is not None
        and _baseline(s)
    ]
    if not real_base:
        log.warning("No data for throughput_vs_size – skipping")
        return

    fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=True, squeeze=False)
    axes = axes[0]

    for panel_idx, (cond_label, cond_fn) in enumerate(conditions):
        ax = axes[panel_idx]

        real = [
            s
            for s in summaries
            if s.simulated_delay_ms is None
            and _nocache(s)
            and s.actual_qps is not None
            and s.actual_qps > 0
            and s.response_size_max is not None
            and cond_fn(s)
        ]

        by_algo: dict[str, list[RunSummary]] = {}
        for s in real:
            by_algo.setdefault(s.algorithm, []).append(s)

        fit_x, fit_y, fit_w = [], [], []

        for algo in ["NONE"] + list(_ALGO_ORDER):
            if algo not in by_algo:
                continue
            runs = by_algo[algo]
            size = runs[0].response_size_max
            qps_vals = [r.actual_qps for r in runs]
            mean_qps = np.mean(qps_vals)
            std_qps = np.std(qps_vals) if len(qps_vals) > 1 else 0
            color = ALGO_COLORS.get(algo, "#888888")
            marker = ALGO_MARKERS.get(algo, "o")
            lbl = ALGO_LABELS.get(algo, algo) if panel_idx == 0 else None
            ax.errorbar(
                size,
                mean_qps,
                yerr=std_qps,
                fmt="none",
                ecolor=color,
                elinewidth=1.2,
                capsize=3,
                capthick=1.2,
            )
            ax.scatter(
                size,
                mean_qps,
                color=color,
                marker=marker,
                edgecolor="black",
                linewidth=1.0,
                s=100,
                zorder=5,
                label=lbl,
            )
            fit_x.append(size)
            fit_y.append(mean_qps)
            fit_w.append(1.0 / std_qps if std_qps > 0 else 1.0)

        # Simulated overlay
        sim_runs = [
            s
            for s in summaries
            if _valid_sim(s)
            and _nocache(s)
            and s.actual_qps is not None
            and s.actual_qps > 0
            and s.response_size_max is not None
            and cond_fn(s)
        ]
        for s in sim_runs:
            is_tcp = TRANSPORT_OVERHEAD_MS.get(s.algorithm, 0) > 0
            ax.scatter(
                s.response_size_max,
                s.actual_qps,
                color=_SIM_COLOR,
                marker=_SIM_MARKER_TCP if is_tcp else _SIM_MARKER_UDP,
                s=_SIM_SIZE,
                edgecolor=_SIM_EDGE,
                linewidth=0.7,
                alpha=0.85,
                zorder=4,
            )
            ax.annotate(
                _sim_label(s),
                (s.response_size_max, s.actual_qps),
                textcoords="offset points",
                xytext=(6, -4),
                fontsize=6,
                color=_SIM_EDGE,
                fontweight="bold",
            )

        # Weighted linear fit
        if len(fit_x) >= 2:
            _add_linear_fit(ax, np.array(fit_x), np.array(fit_y), np.array(fit_w))

        # EDNS(0) threshold
        ax.axvline(
            1232,
            color="#d35400",
            linestyle="--",
            linewidth=1.5,
            alpha=0.7,
            zorder=1,
            label="EDNS(0) 1232 B" if panel_idx == 0 else None,
        )

        ax.set_xscale("log")
        ax.set_xlabel("Signed response size (bytes)", labelpad=10)
        if panel_idx == 0:
            ax.set_ylabel("Achieved QPS", labelpad=20)
        ax.set_title(cond_label, fontsize=11)
        _grid(ax)

    handles, labels_leg = axes[0].get_legend_handles_labels()
    ncol = max(4, (len(handles) + 3) // 4)
    leg = fig.legend(
        handles=handles,
        labels=labels_leg,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.02),
        ncol=ncol,
        frameon=False,
        fontsize=8,
    )
    for t in leg.get_texts():
        t.set_fontweight("bold")
    fig.suptitle("Throughput vs response size (no-cache regime)", fontsize=13, y=1.02)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.20)
    _save(fig, output)


def plot_throughput_vs_signing_time(summaries: list[RunSummary], output: Path):
    """Scatter: achieved QPS vs signing time (ms), two panels.

    Left panel: baseline (local, no netem).
    Right panel: 25 ms one-way delay.
    Reveals whether signing latency is the dominant throughput bottleneck.
    Filtered to no-cache regime.  Shaded convex-hull clusters separate
    UDP and TCP algorithms.
    """
    from scipy.spatial import ConvexHull

    _style()

    def _nocache(s: RunSummary) -> bool:
        return (s.sig_cache_cap or 0) == 0 and int(s.cache_ttl or 0) == 0

    def _baseline(s: RunSummary) -> bool:
        return s.network_delay_ms in (None, 0) and s.network_rate_kbps is None

    def _nd25(s: RunSummary) -> bool:
        return s.network_delay_ms == 25 and s.network_rate_kbps is None

    conditions: list[tuple[str, callable]] = [
        ("Local (no delay)", _baseline),
        ("25 ms delay", _nd25),
    ]

    # Check we have data for at least the baseline panel
    real_base = [
        s
        for s in summaries
        if s.simulated_delay_ms is None
        and _nocache(s)
        and s.actual_qps is not None
        and s.actual_qps > 0
        and s.algorithm in SIGNING_TIMES_MS
        and _baseline(s)
    ]
    if not real_base:
        log.warning("No data for throughput_vs_signing_time – skipping")
        return

    from src.analyzer import TRANSPORT_OVERHEAD_MS

    fig, axes = plt.subplots(1, 2, figsize=(14, 6), sharey=True, squeeze=False)
    axes = axes[0]

    for panel_idx, (cond_label, cond_fn) in enumerate(conditions):
        ax = axes[panel_idx]

        real = [
            s
            for s in summaries
            if s.simulated_delay_ms is None
            and _nocache(s)
            and s.actual_qps is not None
            and s.actual_qps > 0
            and s.algorithm in SIGNING_TIMES_MS
            and cond_fn(s)
        ]

        by_algo: dict[str, list[RunSummary]] = {}
        for s in real:
            by_algo.setdefault(s.algorithm, []).append(s)

        fit_x, fit_y, fit_w = [], [], []
        udp_pts, tcp_pts = [], []

        for algo in ["NONE"] + list(_ALGO_ORDER):
            if algo not in by_algo:
                continue
            is_tcp = TRANSPORT_OVERHEAD_MS.get(algo, 0) > 0
            runs = by_algo[algo]
            sign_ms = SIGNING_TIMES_MS.get(algo, 0)
            qps_vals = [r.actual_qps for r in runs]
            mean_qps = np.mean(qps_vals)
            std_qps = np.std(qps_vals) if len(qps_vals) > 1 else 0
            color = ALGO_COLORS.get(algo, "#888888")
            marker = ALGO_MARKERS.get(algo, "o")
            lbl = ALGO_LABELS.get(algo, algo) if panel_idx == 0 else None
            ax.errorbar(
                sign_ms,
                mean_qps,
                yerr=std_qps,
                fmt="none",
                ecolor=color,
                elinewidth=1.2,
                capsize=3,
                capthick=1.2,
            )
            ax.scatter(
                sign_ms,
                mean_qps,
                color=color,
                marker=marker,
                edgecolor="black",
                linewidth=1.0,
                s=100,
                zorder=5,
                label=lbl,
            )
            fit_x.append(sign_ms)
            fit_y.append(mean_qps)
            fit_w.append(1.0 / std_qps if std_qps > 0 else 1.0)
            bucket = tcp_pts if is_tcp else udp_pts
            bucket.append((np.log10(max(sign_ms, 1e-6)), mean_qps))

        # Simulated overlay
        sim_runs = [
            s
            for s in summaries
            if _valid_sim(s)
            and _nocache(s)
            and s.actual_qps is not None
            and s.actual_qps > 0
            and cond_fn(s)
        ]
        for s in sim_runs:
            base_ms = SIGNING_TIMES_MS.get(s.algorithm, 0)
            eff_ms = base_ms + (s.simulated_delay_ms or 0)
            is_tcp = TRANSPORT_OVERHEAD_MS.get(s.algorithm, 0) > 0
            ax.scatter(
                eff_ms,
                s.actual_qps,
                color=_SIM_COLOR,
                marker=_SIM_MARKER_UDP,
                s=_SIM_SIZE,
                edgecolor=_SIM_EDGE,
                linewidth=0.7,
                alpha=0.85,
                zorder=4,
            )
            ax.annotate(
                _sim_label(s),
                (eff_ms, s.actual_qps),
                textcoords="offset points",
                xytext=(6, -4),
                fontsize=6,
                color=_SIM_EDGE,
                fontweight="bold",
            )
            bucket = tcp_pts if is_tcp else udp_pts
            bucket.append((np.log10(max(eff_ms, 1e-6)), s.actual_qps))

        # ── shaded convex-hull clusters ─────────────────────────────────
        def _draw_cluster(pts_mixed, color, label):
            """Draw convex hull in log-x / linear-y space."""
            if len(pts_mixed) < 3:
                return
            pts = np.array(pts_mixed)
            centroid = pts.mean(axis=0)
            padded = centroid + (pts - centroid) * 1.15
            try:
                hull = ConvexHull(padded)
            except Exception:
                return
            verts = padded[hull.vertices]
            verts = np.vstack([verts, verts[0]])
            verts_x = 10 ** verts[:, 0]
            verts_y = verts[:, 1]
            ax.fill(verts_x, verts_y, color=color, alpha=0.10, zorder=1)
            ax.plot(
                verts_x, verts_y, color=color, alpha=0.35, linewidth=1.5, linestyle="--", zorder=1
            )
            idx = np.argmax(verts[:-1, 0])
            ax.annotate(
                label,
                (verts_x[idx], verts_y[idx]),
                textcoords="offset points",
                xytext=(8, 6),
                fontsize=9,
                fontweight="bold",
                color=color,
                alpha=0.7,
            )

        _draw_cluster(udp_pts, "#2ca02c", "UDP")
        _draw_cluster(tcp_pts, "#d62728", "TCP")

        # Weighted linear fit
        if len(fit_x) >= 2:
            _add_linear_fit(ax, np.array(fit_x), np.array(fit_y), np.array(fit_w))

        ax.set_xscale("log")
        ax.set_xlabel("Signing time (ms)", labelpad=10)
        if panel_idx == 0:
            ax.set_ylabel("Achieved QPS", labelpad=20)
        ax.set_title(cond_label, fontsize=11)
        _grid(ax)

    from matplotlib.patches import Patch

    handles, _ = axes[0].get_legend_handles_labels()
    handles += [
        Patch(
            facecolor="#2ca02c",
            alpha=0.15,
            edgecolor="#2ca02c",
            linewidth=1.5,
            linestyle="--",
            label="UDP cluster",
        ),
        Patch(
            facecolor="#d62728",
            alpha=0.15,
            edgecolor="#d62728",
            linewidth=1.5,
            linestyle="--",
            label="TCP cluster",
        ),
    ]
    ncol = max(4, (len(handles) + 3) // 4)
    leg = fig.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.02),
        ncol=ncol,
        frameon=False,
        fontsize=8,
    )
    for t in leg.get_texts():
        t.set_fontweight("bold")
    fig.suptitle("Throughput vs signing time (no-cache regime)", fontsize=13, y=1.02)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.20)
    _save(fig, output)


def plot_resource_usage(summaries: list[RunSummary], output: Path):
    """CPU millicores and memory (MiB) per algorithm under load.

    Only available for runs collected with container-resource scraping.
    """
    _style()
    real = [
        s for s in summaries if s.simulated_delay_ms is None and s.cpu_millicores_mean is not None
    ]
    if not real:
        log.warning("No container resource data – skipping resource_usage plot")
        return

    algos = [a for a in _ALGO_ORDER if any(s.algorithm == a for s in real)]
    fig, (ax_cpu, ax_mem) = plt.subplots(1, 2, figsize=(14, 6))

    x = np.arange(len(algos))
    width = 0.35

    cpu_means, cpu_maxes, mem_means, mem_maxes = [], [], [], []
    for algo in algos:
        runs = [s for s in real if s.algorithm == algo]
        cpu_means.append(np.mean([s.cpu_millicores_mean for s in runs]))
        cpu_maxes.append(np.mean([s.cpu_millicores_max for s in runs]))
        mem_means.append(np.mean([s.memory_mib_mean for s in runs]))
        mem_maxes.append(np.mean([s.memory_mib_max for s in runs]))

    colors = [ALGO_COLORS.get(a, "grey") for a in algos]
    ax_cpu.bar(
        x - width / 2,
        cpu_means,
        width,
        color=colors,
        edgecolor="black",
        linewidth=0.8,
        label="Mean",
    )
    ax_cpu.bar(
        x + width / 2,
        cpu_maxes,
        width,
        color=colors,
        edgecolor="black",
        linewidth=0.8,
        alpha=0.6,
        label="Max",
    )
    ax_cpu.set_xticks(x)
    ax_cpu.set_xticklabels([ALGO_LABELS.get(a, a) for a in algos], rotation=30, ha="right")
    ax_cpu.set_ylabel("CPU (millicores)", labelpad=20)
    ax_cpu.legend(framealpha=0.9)
    _grid(ax_cpu)

    ax_mem.bar(
        x - width / 2,
        mem_means,
        width,
        color=colors,
        edgecolor="black",
        linewidth=0.8,
        label="Mean",
    )
    ax_mem.bar(
        x + width / 2,
        mem_maxes,
        width,
        color=colors,
        edgecolor="black",
        linewidth=0.8,
        alpha=0.6,
        label="Max",
    )
    ax_mem.set_xticks(x)
    ax_mem.set_xticklabels([ALGO_LABELS.get(a, a) for a in algos], rotation=30, ha="right")
    ax_mem.set_ylabel("Memory (MiB)", labelpad=20)
    ax_mem.legend(framealpha=0.9)
    _grid(ax_mem)

    fig.tight_layout()
    _save(fig, output)


def plot_coalescence_vs_sigma(summaries: list[RunSummary], output: Path):
    """Coalescence rate and deduplication factor vs nominal Σ.

    Shows direct singleflight measurements for all uncached runs (real +
    simulated-delay), demonstrating the negative-feedback mechanism: as Σ
    increases, coalescence rises and the deduplication factor grows, keeping
    effective utilization below 1.

    Left axis: coalescence rate (fraction of queries that shared a leader).
    Right axis: deduplication factor ((execs + coalesced) / execs).
    """
    _style()
    from matplotlib.lines import Line2D

    # Only include uncached runs with singleflight data.
    # Exclude netem/bandwidth-limited runs: Prometheus counters are
    # cumulative and the pod is not redeployed when only network
    # conditions change, so later runs carry over prior counters.
    runs = [
        s
        for s in summaries
        if s.sig_cache_cap == 0
        and int(s.cache_ttl or 0) == 0
        and s.singleflight_execs is not None
        and s.singleflight_execs > 0
        and s.sigma is not None
        and s.network_delay_ms in (None, 0)
        and s.network_rate_kbps is None
    ]
    if not runs:
        log.warning("No singleflight data for uncached runs – skipping coalescence_vs_sigma plot")
        return

    fig, ax1 = plt.subplots()
    ax2 = ax1.twinx()

    for s in runs:
        sf_coal = s.singleflight_coalesced or 0
        sf_exec = s.singleflight_execs
        total = sf_coal + sf_exec
        coal_rate = sf_coal / total if total > 0 else 0
        dedup = total / sf_exec if sf_exec > 0 else 1

        is_sim = s.simulated_delay_ms is not None
        color = _SIM_COLOR if is_sim else ALGO_COLORS.get(s.algorithm, "grey")
        marker = ALGO_MARKERS.get(s.algorithm, "o")
        edge = _SIM_EDGE if is_sim else "black"

        ax1.scatter(
            s.sigma,
            coal_rate,
            color=color,
            marker=marker,
            edgecolor=edge,
            linewidth=0.8,
            s=80,
            zorder=5,
            alpha=0.85,
        )
        ax2.scatter(
            s.sigma,
            dedup,
            color=color,
            marker=marker,
            facecolors="none",
            edgecolor=color,
            linewidth=1.2,
            s=80,
            zorder=4,
            alpha=0.7,
        )

    # Theoretical predictions: renewal-theory coalescence
    sigma_th = np.linspace(min(s.sigma for s in runs) * 0.8, max(s.sigma for s in runs) * 1.1, 200)
    # N_eff=1 curve (upper bound)
    ax1.plot(
        sigma_th,
        sigma_th / (1 + sigma_th),
        color="grey",
        linestyle="--",
        linewidth=1.2,
        alpha=0.5,
        label=r"$N_{\mathrm{eff}}=1$",
    )
    # N_eff=5 curve (matches baseline_services=5)
    N_EFF = 5
    sigma_k = sigma_th / N_EFF
    ax1.plot(
        sigma_th,
        sigma_k / (1 + sigma_k),
        color="#E06040",
        linestyle="-.",
        linewidth=1.5,
        alpha=0.7,
        label=r"$N_{\mathrm{eff}}=5$",
    )

    ax1.set_xlabel(r"Nominal utilization $\Sigma$", labelpad=10)
    ax1.set_ylabel("Coalescence rate", labelpad=15, color="black")
    ax2.set_ylabel("Deduplication factor", labelpad=15, color="grey")
    ax1.set_xscale("log")
    ax2.set_yscale("log")
    ax1.set_ylim(-0.05, 1.05)

    # Build legend from algorithm order
    handles = []
    # Axis encoding legend
    handles.append(
        Line2D(
            [],
            [],
            marker="o",
            color="grey",
            markeredgecolor="black",
            markersize=7,
            linestyle="None",
            markerfacecolor="grey",
            label="Coalescence rate (filled, left)",
        )
    )
    handles.append(
        Line2D(
            [],
            [],
            marker="o",
            color="grey",
            markeredgecolor="grey",
            markersize=7,
            markerfacecolor="none",
            linewidth=1.2,
            linestyle="None",
            label="Dedup. factor (open, right)",
        )
    )
    for algo in _ALGO_ORDER:
        if any(s.algorithm == algo for s in runs if s.simulated_delay_ms is None):
            handles.append(
                Line2D(
                    [],
                    [],
                    marker=ALGO_MARKERS.get(algo, "o"),
                    color=ALGO_COLORS.get(algo, "grey"),
                    markeredgecolor="black",
                    markersize=7,
                    linestyle="None",
                    label=ALGO_LABELS.get(algo, algo),
                )
            )
    if any(s.simulated_delay_ms is not None for s in runs):
        handles.append(
            Line2D(
                [],
                [],
                marker="D",
                color=_SIM_COLOR,
                markeredgecolor=_SIM_EDGE,
                markersize=7,
                linestyle="None",
                label="Simulated delay",
            )
        )
    handles.append(
        Line2D([], [], color="grey", linestyle="--", linewidth=1.2, label=r"$N_{\mathrm{eff}}=1$")
    )
    handles.append(
        Line2D(
            [], [], color="#E06040", linestyle="-.", linewidth=1.5, label=r"$N_{\mathrm{eff}}=5$"
        )
    )
    ax1.legend(handles=handles, loc="upper left", framealpha=0.9, fontsize=8)

    _grid(ax1)
    ax1.set_title("Singleflight coalescence vs. nominal utilization (uncached)")
    _save(fig, output)


def plot_effective_signing_rate(summaries: list[RunSummary], output: Path):
    """Effective signing rate vs nominal Σ in the uncached regime.

    Demonstrates the singleflight self-regulation: as Σ grows, the actual
    number of signing operations per second saturates rather than growing
    linearly.  The effective rate is measured directly from the singleflight
    counter (execs / measurement_duration).
    """
    _style()
    from matplotlib.lines import Line2D

    MEASUREMENT_DURATION = 12.0  # seconds (from benchmark config)

    # Exclude netem/bandwidth-limited runs: cumulative Prometheus
    # counters are not reset between network-condition changes.
    runs = [
        s
        for s in summaries
        if s.sig_cache_cap == 0
        and int(s.cache_ttl or 0) == 0
        and s.singleflight_execs is not None
        and s.singleflight_execs > 0
        and s.sigma is not None
        and s.network_delay_ms in (None, 0)
        and s.network_rate_kbps is None
    ]
    if not runs:
        log.warning(
            "No singleflight data for uncached runs – skipping effective_signing_rate plot"
        )
        return

    fig, ax = plt.subplots()

    for s in runs:
        eff_rate = s.singleflight_execs / MEASUREMENT_DURATION
        is_sim = s.simulated_delay_ms is not None
        color = _SIM_COLOR if is_sim else ALGO_COLORS.get(s.algorithm, "grey")
        marker = ALGO_MARKERS.get(s.algorithm, "o")
        edge = _SIM_EDGE if is_sim else "black"

        ax.scatter(
            s.sigma,
            eff_rate,
            color=color,
            marker=marker,
            edgecolor=edge,
            linewidth=0.8,
            s=80,
            zorder=5,
            alpha=0.85,
        )

    # Reference line: without singleflight, rate = λ_q
    lq = max(s.n_queries / 12.0 for s in runs)  # queries/s from data
    lq_round = round(lq / 10) * 10  # round to nearest 10
    ax.axhline(
        lq_round,
        color="#d35400",
        linestyle="--",
        linewidth=1.2,
        alpha=0.6,
        label=rf"$\lambda_q = {lq_round:.0f}$ qps (no dedup)",
    )

    ax.set_xlabel(r"Nominal utilization $\Sigma$", labelpad=10)
    ax.set_ylabel("Effective signing rate (signs/s)", labelpad=15)
    ax.set_xscale("log")
    ax.set_yscale("log")

    handles = []
    for algo in _ALGO_ORDER:
        if any(s.algorithm == algo for s in runs if s.simulated_delay_ms is None):
            handles.append(
                Line2D(
                    [],
                    [],
                    marker=ALGO_MARKERS.get(algo, "o"),
                    color=ALGO_COLORS.get(algo, "grey"),
                    markeredgecolor="black",
                    markersize=7,
                    linestyle="None",
                    label=ALGO_LABELS.get(algo, algo),
                )
            )
    if any(s.simulated_delay_ms is not None for s in runs):
        handles.append(
            Line2D(
                [],
                [],
                marker="D",
                color=_SIM_COLOR,
                markeredgecolor=_SIM_EDGE,
                markersize=7,
                linestyle="None",
                label="Simulated delay",
            )
        )
    handles.append(
        Line2D(
            [],
            [],
            color="#d35400",
            linestyle="--",
            linewidth=1.2,
            label=rf"$\lambda_q = {lq_round:.0f}$ (no dedup)",
        )
    )
    ax.legend(handles=handles, loc="upper right", framealpha=0.9, fontsize=9)

    _grid(ax)
    ax.set_title("Effective signing rate vs. nominal utilization (uncached)")
    _save(fig, output)


def plot_cache_effectiveness(summaries: list[RunSummary], output: Path):
    """Cache hit rate and singleflight coalescence per algorithm.

    Only available for runs with Prometheus signing metrics.
    Includes simulated-delay runs appended after real algorithms.
    """
    _style()
    # Exclude netem/bandwidth-limited runs: cumulative Prometheus
    # counters are not reset between network-condition changes.
    real = [
        s
        for s in summaries
        if s.simulated_delay_ms is None
        and s.cache_hit_rate is not None
        and s.network_delay_ms in (None, 0)
        and s.network_rate_kbps is None
    ]
    sim = [
        s
        for s in summaries
        if _valid_sim(s)
        and s.cache_hit_rate is not None
        and s.network_delay_ms in (None, 0)
        and s.network_rate_kbps is None
    ]
    if not real:
        log.warning("No cache effectiveness data – skipping cache_effectiveness plot")
        return

    # Build entries: real algos, then simulated runs (grouped by algo+delay)
    entries: list[tuple[str, str, list[RunSummary], bool]] = []
    for algo in _ALGO_ORDER:
        runs = [s for s in real if s.algorithm == algo]
        if runs:
            entries.append((algo, ALGO_LABELS.get(algo, algo), runs, False))
    sim.sort(key=lambda s: (s.algorithm, s.simulated_delay_ms))
    sim_groups: dict[tuple, list[RunSummary]] = {}
    for s in sim:
        sim_groups.setdefault((s.algorithm, s.simulated_delay_ms), []).append(s)
    for key in sorted(sim_groups.keys()):
        grp = sim_groups[key]
        entries.append((f"sim_{key[0]}_{key[1]}", _sim_label(grp[0]), grp, True))

    fig, ax = plt.subplots()
    x = np.arange(len(entries))
    width = 0.35

    hit_rates, coal_rates = [], []
    for key, label, runs, is_sim in entries:
        hit_rates.append(np.mean([s.cache_hit_rate for s in runs]))
        coal = []
        for s in runs:
            sf_coal = s.singleflight_coalesced or 0
            sf_exec = s.singleflight_execs or 0
            total = sf_coal + sf_exec
            coal.append(sf_coal / total if total > 0 else 0)
        coal_rates.append(np.mean(coal) if coal else 0)

    c1, c2 = _CMAP(0.2), _CMAP(0.8)
    # Use goldenrod tints for simulated entries
    c1_sim, c2_sim = "#DAA520", "#F0C050"
    colors_hit = [c1_sim if e[3] else c1 for e in entries]
    colors_coal = [c2_sim if e[3] else c2 for e in entries]

    ax.bar(
        x - width / 2,
        hit_rates,
        width,
        color=colors_hit,
        edgecolor="black",
        linewidth=0.8,
        label="Cache hit rate",
    )
    ax.bar(
        x + width / 2,
        coal_rates,
        width,
        color=colors_coal,
        edgecolor="black",
        linewidth=0.8,
        label="Singleflight coalescence",
    )

    ax.set_xticks(x)
    ax.set_xticklabels([e[1] for e in entries], rotation=40, ha="right", fontsize=9)
    ax.set_ylabel("Rate", labelpad=20)
    ax.set_ylim(0, 1.05)
    ax.legend(loc="upper right", framealpha=0.9, fontsize=10)
    ax.set_title("Cache effectiveness (all caching configurations)")
    _grid(ax)
    _save(fig, output)


def plot_saturation_heatmap(summaries: list[RunSummary], output: Path):
    """Annotated heatmap: failure rate (%) per algorithm × caching config.

    Reveals that saturation (DNS timeouts) concentrates exclusively in
    the sc10000_ttl5 configuration, where aggressive cache churn (TTL=5 s)
    across 10 000 services overwhelms the signing pipeline.  Algorithms
    with either zero signing cost (Baseline) or extreme singleflight
    coalescence (SLH-DSA) survive unscathed.
    """
    _style()

    # Only real (non-simulated) runs
    real = [s for s in summaries if s.simulated_delay_ms is None and np.isfinite(s.failure_rate)]

    # Configs in severity order
    configs = [
        (0, 0, "sc0\nTTL 0"),
        (0, 5, "sc0\nTTL 5"),
        (10000, 0, "sc10k\nTTL 0"),
        (10000, 5, "sc10k\nTTL 5"),
    ]

    # Algorithms: standard order plus Baseline
    algo_list = ["NONE"] + [a for a in _ALGO_ORDER]
    present = {s.algorithm for s in real}
    algo_list = [a for a in algo_list if a in present]

    # Build matrix [algo_idx, config_idx] = failure_rate %
    matrix = np.full((len(algo_list), len(configs)), np.nan)
    for s in real:
        if s.algorithm not in algo_list:
            continue
        ai = algo_list.index(s.algorithm)
        for ci, (sc, ttl, _) in enumerate(configs):
            if (s.sig_cache_cap or 0) == sc and int(s.cache_ttl or 0) == ttl:
                matrix[ai, ci] = s.failure_rate * 100
                break

    fig, ax = plt.subplots(figsize=(7, 8))

    # Custom colormap: white(0%) → amber → red(12%)
    from matplotlib.colors import LinearSegmentedColormap

    sat_cmap = LinearSegmentedColormap.from_list(
        "saturation", ["#ffffff", "#fff3cd", "#f0ad4e", "#d9534f"], N=256
    )
    sat_cmap.set_bad("#eeeeee")

    im = ax.imshow(matrix, cmap=sat_cmap, aspect="auto", vmin=0, vmax=12, interpolation="nearest")

    # Annotate each cell
    for ai in range(len(algo_list)):
        for ci in range(len(configs)):
            val = matrix[ai, ci]
            if np.isnan(val):
                continue
            txt = f"{val:.1f}%" if val > 0 else "0"
            color = "white" if val > 7 else "black"
            weight = "bold" if val > 0 else "normal"
            ax.text(
                ci, ai, txt, ha="center", va="center", fontsize=10, color=color, fontweight=weight
            )

    # Axes
    ax.set_xticks(range(len(configs)))
    ax.set_xticklabels([c[2] for c in configs], fontsize=11)
    ax.set_yticks(range(len(algo_list)))
    ax.set_yticklabels([ALGO_LABELS.get(a, a) for a in algo_list], fontsize=10)
    ax.set_xlabel("Caching configuration", labelpad=10)

    # Colorbar
    cbar = fig.colorbar(im, ax=ax, shrink=0.6, pad=0.08)
    cbar.set_label("DNS timeout rate (%)", fontsize=11)

    ax.set_title("Saturation heatmap (all caching configurations)")
    fig.tight_layout()
    _save(fig, output)


def plot_latency_bubble(summaries: list[RunSummary], output: Path):
    """Bubble chart: signing time (x) vs response size (y), colour = P99 latency.

    Decorrelates the two performance drivers (signing cost and transport
    overhead) that are conflated in the latency-vs-size scatter plot.
    Shows the nocache regime (sc0_ttl0) where both effects are fully visible.
    """
    from src.analyzer import SIGNING_TIMES_MS

    _style()

    def _pick_nocache(runs: list[RunSummary]) -> RunSummary | None:
        for sc, ttl in [(0, 0), (0, 5), (10000, 0), (10000, 5)]:
            for r in runs:
                if (r.sig_cache_cap or 0) == sc and int(r.cache_ttl or 0) == ttl:
                    return r
        return None

    # Collect data points
    xs, ys, cs, markers, colors_edge, labels_pts = [], [], [], [], [], []

    # Real algorithms
    by_algo: dict[str, list[RunSummary]] = {}
    for s in summaries:
        if s.simulated_delay_ms is None and s.response_size_max is not None:
            by_algo.setdefault(s.algorithm, []).append(s)

    for algo in ["NONE"] + list(_ALGO_ORDER):
        if algo not in by_algo:
            continue
        r = _pick_nocache(by_algo[algo])
        if r is None:
            continue
        st = SIGNING_TIMES_MS.get(algo, 0)
        xs.append(st)
        ys.append(r.response_size_max)
        cs.append(r.latency_p99)
        markers.append(ALGO_MARKERS.get(algo, "o"))
        colors_edge.append("black")
        labels_pts.append(ALGO_LABELS.get(algo, algo))

    # Simulated runs
    sim_runs = [s for s in summaries if _valid_sim(s) and s.response_size_max is not None]
    sim_by_key: dict[tuple, list[RunSummary]] = {}
    for s in sim_runs:
        sim_by_key.setdefault((s.algorithm, s.simulated_delay_ms), []).append(s)
    for (algo, delay), runs in sim_by_key.items():
        r = _pick_nocache(runs) or runs[0]
        eff_st = SIGNING_TIMES_MS.get(algo, 0) + delay
        xs.append(eff_st)
        ys.append(r.response_size_max)
        cs.append(r.latency_p99)
        markers.append(_SIM_MARKER_UDP)
        colors_edge.append(_SIM_EDGE)
        labels_pts.append(_sim_label(r))

    if not xs:
        log.warning("No data for latency_bubble – skipping")
        return

    fig, ax = plt.subplots()

    # Use log-normalised colormap for latency
    import matplotlib.cm as cm

    c_arr = np.array(cs)
    norm = mcolors.LogNorm(vmin=max(c_arr.min(), 0.01), vmax=c_arr.max())
    cmap = cm.RdYlGn_r

    # Plot each point individually (different markers per algo)
    for i in range(len(xs)):
        ax.scatter(
            xs[i],
            ys[i],
            c=[cs[i]],
            cmap=cmap,
            norm=norm,
            marker=markers[i],
            s=180,
            edgecolor=colors_edge[i],
            linewidth=1.0,
            zorder=5,
        )
        ax.annotate(
            labels_pts[i],
            (xs[i], ys[i]),
            textcoords="offset points",
            xytext=(6, 4),
            fontsize=6,
            fontweight="bold",
            color=colors_edge[i],
            alpha=0.8,
        )

    sm = cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, pad=0.02)
    cbar.set_label("P99 latency (ms)", fontweight="bold")

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Effective signing time (ms)", labelpad=10)
    ax.set_ylabel("Signed response size (bytes)", labelpad=20)

    # EDNS(0) threshold horizontal line
    ax.axhline(
        1232,
        color="#d35400",
        linestyle="--",
        linewidth=1.5,
        alpha=0.7,
        zorder=1,
        label="EDNS(0) 1232 B",
    )
    ax.legend(loc="upper left", framealpha=0.9, fontsize=9)

    ax.set_title("Latency decorrelation (no-cache regime)")
    _grid(ax)
    _save(fig, output)


def plot_latency_vs_size(summaries: list[RunSummary], output: Path):
    """Scatter: P99 latency vs signed response size (bytes).

    Directly evidences the claim that signature compactness dominates
    performance differentiation.  The EDNS(0) 1232-byte threshold is
    drawn as a vertical line: algorithms above it fall back to TCP,
    incurring a ~1 ms transport penalty plus higher tail latency.

    Shows the cached regime (sc10000_ttl5) where cache neutralizes
    signing cost, leaving transport as the primary differentiator.
    """
    from src.analyzer import TRANSPORT_OVERHEAD_MS

    _style()
    fig, ax = plt.subplots()

    # Prefer cached regime (sc10000_ttl5); fall back to sc0_ttl5
    def _pick_run(runs: list[RunSummary]) -> RunSummary | None:
        for sc, ttl in [(10000, 5), (0, 5), (10000, 0), (0, 0)]:
            for r in runs:
                if (r.sig_cache_cap or 0) == sc and int(r.cache_ttl or 0) == ttl:
                    return r
        return None

    # Aggregate per algorithm
    by_algo: dict[str, list[RunSummary]] = {}
    for s in summaries:
        if s.simulated_delay_ms is None and s.response_size_max is not None:
            by_algo.setdefault(s.algorithm, []).append(s)

    for algo in ["NONE"] + list(_ALGO_ORDER):
        if algo not in by_algo:
            continue
        r = _pick_run(by_algo[algo])
        if r is None:
            continue
        color = ALGO_COLORS.get(algo, "#888888")
        marker = ALGO_MARKERS.get(algo, "o")
        label = ALGO_LABELS.get(algo, algo)
        ax.scatter(
            r.response_size_max,
            r.latency_p99,
            color=color,
            marker=marker,
            edgecolor="black",
            linewidth=1.2,
            s=120,
            zorder=5,
            label=label,
        )
        # No in-plot annotations for real algorithms (legend suffices)

    # ── simulated overlay ───────────────────────────────────────────────
    sim_runs = [s for s in summaries if _valid_sim(s) and s.response_size_max is not None]
    # For simulated runs, pick the cached config too
    sim_by_key: dict[tuple, list[RunSummary]] = {}
    for s in sim_runs:
        sim_by_key.setdefault((s.algorithm, s.simulated_delay_ms), []).append(s)
    for (algo, delay), runs in sim_by_key.items():
        r = _pick_run(runs) or runs[0]
        is_tcp = TRANSPORT_OVERHEAD_MS.get(algo, 0) > 0
        ax.scatter(
            r.response_size_max,
            r.latency_p99,
            color=_SIM_COLOR,
            marker=_SIM_MARKER_TCP if is_tcp else _SIM_MARKER_UDP,
            s=_SIM_SIZE,
            edgecolor=_SIM_EDGE,
            linewidth=0.7,
            alpha=0.85,
            zorder=4,
        )
        ax.annotate(
            _sim_label(r),
            (r.response_size_max, r.latency_p99),
            textcoords="offset points",
            xytext=(6, -4),
            fontsize=6,
            color=_SIM_EDGE,
            fontweight="bold",
        )

    # EDNS(0) threshold
    ax.axvline(
        1232,
        color="#d35400",
        linestyle="--",
        linewidth=1.5,
        alpha=0.7,
        zorder=1,
        label="EDNS(0) 1232 B (UDP→TCP)",
    )

    # Force x-limits to encompass all data with margin, then shade
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.autoscale_view()
    xlim = ax.get_xlim()
    ylim = ax.get_ylim()
    # Extend right xlim to add margin beyond rightmost point
    xlim_right = xlim[1] * 1.3
    ax.set_xlim(xlim[0], xlim_right)
    ax.axvspan(xlim[0], 1232, alpha=0.04, color="#9fcf69", zorder=0)
    ax.axvspan(1232, xlim_right, alpha=0.04, color="#d35400", zorder=0)
    ax.text(
        600,
        ylim[1] * 0.85,
        "UDP",
        fontsize=14,
        ha="center",
        color="#9fcf69",
        fontweight="bold",
        alpha=0.5,
    )
    ax.text(
        4000,
        ylim[1] * 0.85,
        "TCP",
        fontsize=14,
        ha="center",
        color="#d35400",
        fontweight="bold",
        alpha=0.5,
    )

    ax.set_xlabel("Signed response size (bytes)", labelpad=10)
    ax.set_ylabel("P99 latency (ms)", labelpad=20)
    _draw_baseline(ax, _baseline_p99(summaries))

    from matplotlib.lines import Line2D

    handles = [
        Line2D(
            [],
            [],
            marker=ALGO_MARKERS.get(a, "o"),
            color=ALGO_COLORS.get(a, "#888"),
            markeredgecolor="black",
            linestyle="none",
            markersize=8,
            label=ALGO_LABELS.get(a, a),
        )
        for a in (["NONE"] + list(_ALGO_ORDER))
        if a in by_algo
    ]
    handles.append(
        Line2D(
            [0],
            [0],
            color="#d35400",
            linestyle="--",
            linewidth=1.5,
            alpha=0.7,
            label="EDNS(0) 1232 B",
        )
    )
    if sim_runs:
        handles += [
            Line2D(
                [],
                [],
                marker=_SIM_MARKER_UDP,
                color=_SIM_COLOR,
                markeredgecolor=_SIM_EDGE,
                linestyle="none",
                markersize=6,
                label="Simulated (UDP)",
            ),
            Line2D(
                [],
                [],
                marker=_SIM_MARKER_TCP,
                color=_SIM_COLOR,
                markeredgecolor=_SIM_EDGE,
                linestyle="none",
                markersize=6,
                label="Simulated (TCP)",
            ),
        ]
    ncol = max(3, (len(handles) + 2) // 3)
    ax.legend(
        handles=handles,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.18),
        ncol=ncol,
        frameon=False,
        fontsize=8,
    )
    ax.set_title("P99 latency vs response size (cached regime)")
    _grid(ax)
    fig.subplots_adjust(bottom=0.30)
    _save(fig, output)


# ── netem / network delay plots ─────────────────────────────────────────


def _has_netem_data(summaries: list[RunSummary]) -> bool:
    """True if the campaign includes runs with varying network_delay_ms."""
    nd_vals = {s.network_delay_ms for s in summaries if s.network_delay_ms is not None}
    return len(nd_vals) >= 2


def _has_bandwidth_data(summaries: list[RunSummary]) -> bool:
    """True if the campaign includes runs with bandwidth-limited conditions."""
    nr_vals = {
        s.network_rate_kbps
        for s in summaries
        if s.network_rate_kbps is not None and s.network_rate_kbps > 0
    }
    return len(nr_vals) >= 1


def plot_latency_vs_bandwidth(summaries: list[RunSummary], output: Path):
    """P99 latency vs response size at different bandwidth levels.

    Reveals that bandwidth-limited links make every byte of signature
    matter: transfer_time = response_size × 8 / rate.  With unlimited
    bandwidth the size effect is binary (UDP vs TCP at 1232 B); with
    throttled bandwidth it becomes a continuous, linear relationship.

    Left panel:  P99 vs response size coloured by algorithm, faceted by rate.
    Right panel: Normalised slowdown vs signature size per bandwidth level
                 with theoretical overlay.
    """
    from src.analyzer import TRANSPORT_OVERHEAD_MS

    bw_runs = [
        s
        for s in summaries
        if s.network_rate_kbps is not None
        and s.network_rate_kbps > 0
        and s.response_size_max is not None
    ]
    if not bw_runs:
        log.warning("No bandwidth-limited data – skipping plot_latency_vs_bandwidth")
        return

    _style()

    rates = sorted({s.network_rate_kbps for s in bw_runs})
    ncols = len(rates)
    fig, axes = plt.subplots(1, ncols, figsize=(5 * ncols, 5.5), sharey=True, squeeze=False)
    axes = axes[0]

    for idx, rate in enumerate(rates):
        ax = axes[idx]
        runs_at_rate = [s for s in bw_runs if s.network_rate_kbps == rate]

        # Group by algorithm
        by_algo: dict[str, list[RunSummary]] = {}
        for s in runs_at_rate:
            by_algo.setdefault(s.algorithm, []).append(s)

        for algo in list(_ALGO_ORDER) + ["NONE"]:
            if algo not in by_algo:
                continue
            rs = by_algo[algo]
            size = rs[0].response_size_max
            p99 = np.mean([r.latency_p99 for r in rs])
            color = ALGO_COLORS.get(algo, "#888888")
            marker = ALGO_MARKERS.get(algo, "o")
            label = ALGO_LABELS.get(algo, algo) if idx == 0 else None
            is_tcp = TRANSPORT_OVERHEAD_MS.get(algo, 0) > 0
            edge = "black" if is_tcp else "grey"
            ax.scatter(
                size,
                p99,
                color=color,
                marker=marker,
                edgecolor=edge,
                linewidth=1.2 if is_tcp else 0.6,
                s=120,
                zorder=5,
                label=label,
            )

        # Theoretical overlay: L = RTT_transport + size_bytes * 8 / (rate * 1000) * 1000
        #   = RTT_transport + size * 8 / rate  (ms, rate in kbps)
        # Get representative delay from the runs
        delay_ms = np.mean(
            [s.network_delay_ms for s in runs_at_rate if s.network_delay_ms is not None] or [0]
        )
        rtt = 2 * delay_ms
        sizes_th = np.linspace(50, 10000, 200)
        transfer_ms = sizes_th * 8 / rate  # rate in kbps → bits/ms
        ax.plot(
            sizes_th,
            rtt + transfer_ms,
            color="steelblue",
            linestyle="--",
            linewidth=1.2,
            alpha=0.6,
            label="UDP: 1 RTT + L/BW" if idx == 0 else None,
        )
        ax.plot(
            sizes_th,
            3 * rtt + transfer_ms,
            color="darkorange",
            linestyle=":",
            linewidth=1.2,
            alpha=0.6,
            label="TCP: 3 RTT + L/BW" if idx == 0 else None,
        )

        ax.axvline(1232, color="#d35400", linestyle="-.", linewidth=1, alpha=0.4, zorder=1)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("Signed response size (B)", labelpad=8)
        if idx == 0:
            ax.set_ylabel("P99 latency (ms)", labelpad=16)
        ax.set_title(f"{rate} Kbps", fontsize=11)
        _grid(ax)

    # Shared legend below
    handles, labels_leg = axes[0].get_legend_handles_labels()
    ncol = max(3, (len(handles) + 2) // 3)
    fig.legend(
        handles=handles,
        labels=labels_leg,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.02),
        ncol=ncol,
        frameon=False,
        fontsize=7.5,
    )
    fig.suptitle("Bandwidth effect: signature size → latency", fontsize=13, y=1.02)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.22)
    _save(fig, output)


def plot_latency_vs_network_delay(summaries: list[RunSummary], output: Path):
    """Lines: P99 latency vs one-way network delay, one line per algorithm.

    Reveals that for algorithms whose signed response exceeds the EDNS(0)
    1232-byte threshold (TCP fallback), latency scales as ~3 × RTT while
    UDP algorithms scale as ~1 × RTT.  This makes signature compactness
    the dominant performance factor under realistic network conditions.
    """
    from src.analyzer import TRANSPORT_OVERHEAD_MS

    netem_runs = [
        s for s in summaries if s.network_delay_ms is not None and s.simulated_delay_ms is None
    ]
    if not netem_runs:
        log.warning("No netem data for latency_vs_network_delay – skipping")
        return

    _style()
    fig, ax = plt.subplots()

    # Group by algorithm → sorted by delay
    by_algo: dict[str, list[RunSummary]] = {}
    for s in netem_runs:
        by_algo.setdefault(s.algorithm, []).append(s)

    for algo in list(_ALGO_ORDER) + ["NONE"]:
        if algo not in by_algo:
            continue
        runs = sorted(by_algo[algo], key=lambda s: s.network_delay_ms)
        # Aggregate by delay (average across repetitions / cache configs)
        delay_groups: dict[int, list[float]] = {}
        for r in runs:
            delay_groups.setdefault(r.network_delay_ms, []).append(r.latency_p99)
        delays = sorted(delay_groups.keys())
        means = [np.mean(delay_groups[d]) for d in delays]
        stds = [np.std(delay_groups[d]) for d in delays]

        color = ALGO_COLORS.get(algo, "#888888")
        marker = ALGO_MARKERS.get(algo, "o")
        label = ALGO_LABELS.get(algo, algo)
        is_tcp = TRANSPORT_OVERHEAD_MS.get(algo, 0) > 0
        linestyle = "--" if is_tcp else "-"

        ax.errorbar(
            delays,
            means,
            yerr=stds,
            color=color,
            marker=marker,
            markersize=7,
            markeredgecolor="black",
            markeredgewidth=0.8,
            linewidth=2 if is_tcp else 1.5,
            linestyle=linestyle,
            capsize=3,
            capthick=1,
            label=label,
            zorder=5,
        )

    ax.set_xlim(-1, 30)
    ax.set_xlabel("One-way network delay (ms)", labelpad=10)
    ax.set_ylabel("P99 latency (ms)", labelpad=20)
    ax.set_yscale("log")
    ax.set_title("Latency vs network delay (signature size effect)")

    handles, labels_leg = ax.get_legend_handles_labels()
    ncol = max(3, (len(handles) + 2) // 3)
    ax.legend(
        handles=handles,
        labels=labels_leg,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.18),
        ncol=ncol,
        frameon=False,
        fontsize=8,
    )
    _grid(ax)
    fig.subplots_adjust(bottom=0.30)
    _save(fig, output)


def _classify_network_condition(s: RunSummary) -> str | None:
    """Return a human-readable label for the network condition of a run."""
    nd = s.network_delay_ms if s.network_delay_ms is not None else 0
    nr = s.network_rate_kbps
    if nr is not None and nr > 0:
        return f"{nd} ms + {nr} Kbps"
    if nd == 0:
        return "Local (no delay)"
    return f"{nd} ms delay"


def _network_cond_sort_key(label: str) -> tuple:
    """Sort key so conditions order: Local first, then by delay, then BW."""
    if label.startswith("Local"):
        return (0, 0, 0)
    if "Kbps" in label:
        return (2, 0, 0)
    # "N ms delay"
    try:
        ms = int(label.split()[0])
    except ValueError:
        ms = 999
    return (1, ms, 0)


def plot_signing_impact_by_network(summaries: list[RunSummary], output: Path):
    """P99 latency vs signing time, one panel per network condition.

    Reveals how network stress amplifies signing-cost differences:
    at zero delay all algorithms cluster near zero, but under realistic
    network conditions the TCP fallback for large-signature algorithms
    makes signing time a secondary factor while transport dominates.
    """
    from src.analyzer import TRANSPORT_OVERHEAD_MS

    netem_runs = [
        s
        for s in summaries
        if s.network_delay_ms is not None
        and s.simulated_delay_ms is None
        and s.algorithm != "NONE"
    ]
    if not netem_runs:
        log.warning("No netem data for signing_impact_by_network – skipping")
        return

    _style()

    # Build condition → runs mapping (ordered)
    cond_runs: dict[str, list[RunSummary]] = {}
    for s in netem_runs:
        label = _classify_network_condition(s)
        cond_runs.setdefault(label, []).append(s)
    conditions = sorted(cond_runs.keys(), key=_network_cond_sort_key)

    ncols = len(conditions)
    fig, axes = plt.subplots(1, ncols, figsize=(5.5 * ncols, 5.5), sharey=True, squeeze=False)
    axes = axes[0]

    for idx, cond in enumerate(conditions):
        ax = axes[idx]
        by_algo: dict[str, list[RunSummary]] = {}
        for s in cond_runs[cond]:
            by_algo.setdefault(s.algorithm, []).append(s)

        for algo in list(_ALGO_ORDER):
            if algo not in by_algo:
                continue
            rs = by_algo[algo]
            s_bar = SIGNING_TIMES_MS.get(algo, 0)
            p99 = np.mean([r.latency_p99 for r in rs])
            color = ALGO_COLORS.get(algo, "#888888")
            marker = ALGO_MARKERS.get(algo, "o")
            lbl = ALGO_LABELS.get(algo, algo) if idx == 0 else None
            is_tcp = TRANSPORT_OVERHEAD_MS.get(algo, 0) > 0
            edge = "black" if is_tcp else "grey"
            ax.scatter(
                s_bar,
                p99,
                color=color,
                marker=marker,
                edgecolor=edge,
                linewidth=1.2 if is_tcp else 0.6,
                s=120,
                zorder=5,
                label=lbl,
            )

        ax.set_xlabel("Mean signing time (ms)", labelpad=8)
        if idx == 0:
            ax.set_ylabel("P99 latency (ms)", labelpad=16)
        ax.set_title(cond, fontsize=11)
        ax.set_xscale("symlog", linthresh=0.01)
        _grid(ax)

    handles, labels_leg = axes[0].get_legend_handles_labels()
    ncol = max(3, (len(handles) + 2) // 3)
    fig.legend(
        handles=handles,
        labels=labels_leg,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.02),
        ncol=ncol,
        frameon=False,
        fontsize=7.5,
    )
    fig.suptitle("Signing time impact under network conditions", fontsize=13, y=1.02)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.22)
    _save(fig, output)


def plot_size_impact_by_network(summaries: list[RunSummary], output: Path):
    """P99 latency vs response size, one panel per network condition.

    Shows how the UDP/TCP threshold (1232 B) creates a step function
    under local conditions, but under WAN delay the step becomes a
    multiplicative penalty (3× RTT).  Under bandwidth constraints the
    relationship becomes continuous and linear in size.
    """
    from src.analyzer import TRANSPORT_OVERHEAD_MS

    netem_runs = [
        s
        for s in summaries
        if s.network_delay_ms is not None
        and s.simulated_delay_ms is None
        and s.response_size_max is not None
        and s.algorithm != "NONE"
    ]
    if not netem_runs:
        log.warning("No netem data for size_impact_by_network – skipping")
        return

    _style()

    cond_runs: dict[str, list[RunSummary]] = {}
    for s in netem_runs:
        label = _classify_network_condition(s)
        cond_runs.setdefault(label, []).append(s)
    conditions = sorted(cond_runs.keys(), key=_network_cond_sort_key)

    ncols = len(conditions)
    fig, axes = plt.subplots(1, ncols, figsize=(5.5 * ncols, 5.5), sharey=True, squeeze=False)
    axes = axes[0]

    for idx, cond in enumerate(conditions):
        ax = axes[idx]
        runs = cond_runs[cond]
        by_algo: dict[str, list[RunSummary]] = {}
        for s in runs:
            by_algo.setdefault(s.algorithm, []).append(s)

        for algo in list(_ALGO_ORDER):
            if algo not in by_algo:
                continue
            rs = by_algo[algo]
            size = rs[0].response_size_max
            p99 = np.mean([r.latency_p99 for r in rs])
            color = ALGO_COLORS.get(algo, "#888888")
            marker = ALGO_MARKERS.get(algo, "o")
            lbl = ALGO_LABELS.get(algo, algo) if idx == 0 else None
            is_tcp = TRANSPORT_OVERHEAD_MS.get(algo, 0) > 0
            edge = "black" if is_tcp else "grey"
            ax.scatter(
                size,
                p99,
                color=color,
                marker=marker,
                edgecolor=edge,
                linewidth=1.2 if is_tcp else 0.6,
                s=120,
                zorder=5,
                label=lbl,
            )

        # Theoretical overlays for bandwidth-limited conditions
        nd = np.mean([s.network_delay_ms for s in runs if s.network_delay_ms is not None] or [0])
        rtt = 2 * nd
        nr_vals = [
            s.network_rate_kbps
            for s in runs
            if s.network_rate_kbps is not None and s.network_rate_kbps > 0
        ]
        if nr_vals:
            rate = np.mean(nr_vals)
            sizes_th = np.linspace(50, 10000, 200)
            transfer_ms = sizes_th * 8 / rate
            ax.plot(
                sizes_th,
                rtt + transfer_ms,
                color="steelblue",
                linestyle="--",
                linewidth=1.2,
                alpha=0.6,
            )
            ax.plot(
                sizes_th,
                3 * rtt + transfer_ms,
                color="darkorange",
                linestyle=":",
                linewidth=1.2,
                alpha=0.6,
            )

        ax.axvline(1232, color="#d35400", linestyle="-.", linewidth=1, alpha=0.4, zorder=1)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("Signed response size (B)", labelpad=8)
        if idx == 0:
            ax.set_ylabel("P99 latency (ms)", labelpad=16)
        ax.set_title(cond, fontsize=11)
        _grid(ax)

    handles, labels_leg = axes[0].get_legend_handles_labels()
    ncol = max(3, (len(handles) + 2) // 3)
    fig.legend(
        handles=handles,
        labels=labels_leg,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.02),
        ncol=ncol,
        frameon=False,
        fontsize=7.5,
    )
    fig.suptitle("Response size impact under network conditions", fontsize=13, y=1.02)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.22)
    _save(fig, output)


def plot_network_penalty_bars(summaries: list[RunSummary], output: Path):
    """Grouped bar chart: P99 per algorithm under each network condition.

    Side-by-side bars make it visually immediate which algorithms suffer
    most when network delay or bandwidth constraints are introduced.
    TCP-fallback algorithms (large signatures) show disproportionate
    increases, while compact-signature algorithms remain resilient.
    """
    netem_runs = [
        s
        for s in summaries
        if s.network_delay_ms is not None
        and s.simulated_delay_ms is None
        and s.algorithm != "NONE"
    ]
    if not netem_runs:
        log.warning("No netem data for network_penalty_bars – skipping")
        return

    _style()

    cond_runs: dict[str, list[RunSummary]] = {}
    for s in netem_runs:
        label = _classify_network_condition(s)
        cond_runs.setdefault(label, []).append(s)
    conditions = sorted(cond_runs.keys(), key=_network_cond_sort_key)

    algos = [a for a in _ALGO_ORDER if any(s.algorithm == a for s in netem_runs)]

    fig, ax = plt.subplots(figsize=(14, 6))
    n_cond = len(conditions)
    if n_cond == 0:
        plt.close(fig)
        return
    width = 0.8 / n_cond
    x = np.arange(len(algos))

    cond_colors = list(_CMAP(np.linspace(0.0, 1.0, max(n_cond, 2))))

    for i, cond in enumerate(conditions):
        vals = []
        for algo in algos:
            matches = [s.latency_p99 for s in cond_runs[cond] if s.algorithm == algo]
            vals.append(np.mean(matches) if matches else 0)
        ax.bar(
            x + i * width,
            vals,
            width,
            label=cond,
            color=cond_colors[i],
            edgecolor="black",
            linewidth=0.6,
        )

    ax.set_xticks(x + width * (n_cond - 1) / 2)
    ax.set_xticklabels([ALGO_LABELS.get(a, a) for a in algos], rotation=30, ha="right")
    ax.set_ylabel("P99 latency (ms)", labelpad=16)
    ax.set_yscale("log")
    ax.set_title("Network condition impact per algorithm")
    _draw_baseline(ax, _baseline_p99(summaries))
    ax.legend(title="Network condition", loc="upper left", framealpha=0.9, fontsize=8)
    _grid(ax)
    fig.tight_layout()
    _save(fig, output)


def plot_signing_regime_by_network(summaries: list[RunSummary], output: Path):
    """Signing regime identification faceted by network condition.

    One panel per network condition (Local / 25 ms delay / 500 Kbps).
    Each panel shows P99 vs Σ for no-cache runs, with UDP/TCP convex-hull
    clusters.  Reveals how the transport penalty (TCP 3-RTT) widens the
    gap between UDP and TCP clusters as network delay or bandwidth
    constraints increase — making signature compactness the dominant
    performance factor under realistic WAN conditions.
    """
    from scipy.spatial import ConvexHull
    from src.analyzer import TRANSPORT_OVERHEAD_MS

    no_cache = [
        s
        for s in summaries
        if s.sig_cache_cap == 0
        and (s.cache_ttl is not None and s.cache_ttl == 0)
        and s.sigma is not None
        and s.network_delay_ms is not None
        and s.simulated_delay_ms is None
        and s.algorithm != "NONE"
        and (s.network_rate_kbps is None or s.network_rate_kbps == 0)
    ]
    if not no_cache:
        log.warning("No no-cache netem data for signing_regime_by_network – skipping")
        return

    # Simulated-delay runs (same filters, but _valid_sim instead)
    sim_no_cache = [
        s
        for s in summaries
        if s.sig_cache_cap == 0
        and (s.cache_ttl is not None and s.cache_ttl == 0)
        and s.sigma is not None
        and s.network_delay_ms is not None
        and _valid_sim(s)
        and s.algorithm != "NONE"
        and (s.network_rate_kbps is None or s.network_rate_kbps == 0)
    ]
    sim_cond_runs: dict[str, list[RunSummary]] = {}
    for s in sim_no_cache:
        label = _classify_network_condition(s)
        sim_cond_runs.setdefault(label, []).append(s)

    _style()

    cond_runs: dict[str, list[RunSummary]] = {}
    for s in no_cache:
        label = _classify_network_condition(s)
        cond_runs.setdefault(label, []).append(s)
    conditions = sorted(cond_runs.keys(), key=_network_cond_sort_key)

    ncols = len(conditions)
    fig, axes = plt.subplots(1, ncols, figsize=(6.5 * ncols, 6), sharey=True, squeeze=False)
    axes = axes[0]

    for idx, cond in enumerate(conditions):
        ax = axes[idx]
        runs = cond_runs[cond]

        by_algo: dict[str, list[RunSummary]] = {}
        for s in runs:
            by_algo.setdefault(s.algorithm, []).append(s)

        udp_pts, tcp_pts = [], []

        for algo in _ALGO_ORDER:
            if algo not in by_algo:
                continue
            is_tcp = TRANSPORT_OVERHEAD_MS.get(algo, 0) > 0
            algo_runs = sorted(by_algo[algo], key=lambda r: r.sigma)
            sigmas = [r.sigma for r in algo_runs]
            p99s = [r.latency_p99 for r in algo_runs]

            lbl = ALGO_LABELS.get(algo, algo) if idx == 0 else None
            ax.scatter(
                sigmas,
                p99s,
                color=ALGO_COLORS[algo],
                marker=ALGO_MARKERS[algo],
                edgecolor="black",
                linewidth=1.2,
                s=120,
                label=lbl,
                zorder=5,
                alpha=0.8,
            )
            if len(sigmas) > 1:
                ax.plot(
                    sigmas, p99s, color=ALGO_COLORS[algo], alpha=0.4, linewidth=1.2, linestyle="--"
                )

            bucket = tcp_pts if is_tcp else udp_pts
            for sig, p99 in zip(sigmas, p99s):
                if sig > 0 and p99 > 0:
                    bucket.append((np.log10(sig), np.log10(p99)))

        # Simulated-delay overlay
        for s in sim_cond_runs.get(cond, []):
            is_tcp = TRANSPORT_OVERHEAD_MS.get(s.algorithm, 0) > 0
            ax.scatter(
                s.sigma,
                s.latency_p99,
                color=_SIM_COLOR,
                marker=_SIM_MARKER_TCP if is_tcp else _SIM_MARKER_UDP,
                s=_SIM_SIZE,
                edgecolor=_SIM_EDGE,
                linewidth=0.7,
                alpha=0.85,
                zorder=4,
            )
            ax.annotate(
                _sim_label(s),
                (s.sigma, s.latency_p99),
                textcoords="offset points",
                xytext=(6, -4),
                fontsize=6,
                color=_SIM_EDGE,
                fontweight="bold",
            )
            bucket = tcp_pts if is_tcp else udp_pts
            if s.sigma > 0 and s.latency_p99 > 0:
                bucket.append((np.log10(s.sigma), np.log10(s.latency_p99)))

        # Draw convex-hull clusters
        def _draw_cluster(pts_log, color, label):
            if len(pts_log) < 3:
                return
            pts = np.array(pts_log)
            centroid = pts.mean(axis=0)
            padded = centroid + (pts - centroid) * 1.15
            try:
                hull = ConvexHull(padded)
            except Exception:
                return
            verts_log = padded[hull.vertices]
            verts_log = np.vstack([verts_log, verts_log[0]])
            verts_x = 10 ** verts_log[:, 0]
            verts_y = 10 ** verts_log[:, 1]
            ax.fill(verts_x, verts_y, color=color, alpha=0.10, zorder=1)
            ax.plot(
                verts_x, verts_y, color=color, alpha=0.35, linewidth=1.5, linestyle="--", zorder=1
            )
            top_idx = np.argmax(verts_log[:-1, 0] + verts_log[:-1, 1])
            ax.annotate(
                label,
                (verts_x[top_idx], verts_y[top_idx]),
                textcoords="offset points",
                xytext=(8, 6),
                fontsize=9,
                fontweight="bold",
                color=color,
                alpha=0.7,
            )

        _draw_cluster(udp_pts, "#2ca02c", "UDP")
        _draw_cluster(tcp_pts, "#d62728", "TCP")

        ax.axvline(x=1, color="grey", linestyle=":", linewidth=1.5, alpha=0.7)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel(r"Cryptographic load $\Sigma = \lambda_q \cdot \bar{s}$", labelpad=8)
        if idx == 0:
            ax.set_ylabel("P99 latency (ms)", labelpad=16)
        ax.set_title(cond, fontsize=11)
        _grid(ax)

    # Shared legend below
    handles, labels_leg = axes[0].get_legend_handles_labels()
    from matplotlib.patches import Patch

    handles += [
        Patch(
            facecolor="#2ca02c",
            alpha=0.15,
            edgecolor="#2ca02c",
            linewidth=1.5,
            linestyle="--",
            label="UDP cluster",
        ),
        Patch(
            facecolor="#d62728",
            alpha=0.15,
            edgecolor="#d62728",
            linewidth=1.5,
            linestyle="--",
            label="TCP cluster",
        ),
    ]
    labels_leg += ["UDP cluster", "TCP cluster"]
    ncol = max(3, (len(handles) + 2) // 3)
    fig.legend(
        handles=handles,
        labels=labels_leg,
        loc="upper center",
        bbox_to_anchor=(0.5, -0.02),
        ncol=ncol,
        frameon=False,
        fontsize=8,
    )
    fig.suptitle("Signing regime under network conditions (no-cache)", fontsize=13, y=1.02)
    fig.tight_layout()
    fig.subplots_adjust(bottom=0.14)
    _save(fig, output)


def _dedup_runs(summaries: list[RunSummary]) -> list[RunSummary]:
    """Keep one run per unique config, picking the one with the most queries."""
    from collections import defaultdict

    by_key: dict[tuple, list[RunSummary]] = defaultdict(list)
    for s in summaries:
        key = (
            s.algorithm,
            s.churn_rate,
            s.query_rate,
            s.sig_cache_cap or 0,
            s.cache_ttl or 0,
            s.simulated_delay_ms or 0,
            s.network_delay_ms or 0,
            s.network_rate_kbps or 0,
        )
        by_key[key].append(s)
    deduped = []
    for runs in by_key.values():
        best = max(runs, key=lambda r: r.n_queries)
        deduped.append(best)
    return deduped


def generate_all_plots(summaries: list[RunSummary], output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    # Keep one run per config (the one with the most queries).
    summaries = _dedup_runs(summaries)
    log.info("After dedup: %d unique configs", len(summaries))
    # Filter out runs with invalid latency data (e.g. 100% timeout → NaN P99).
    # High error rates for slow algorithms (e.g. SLH-DSA at σ >> 1) are
    # expected saturation behaviour, not cluster instability.
    valid = [s for s in summaries if np.isfinite(s.latency_p99) and s.latency_p99 > 0]
    log.info(
        "Plotting with %d/%d valid runs (dropped %d with NaN/zero P99)",
        len(valid),
        len(summaries),
        len(summaries) - len(valid),
    )
    plot_latency_vs_sigma(valid, output_dir / "latency_vs_sigma.pdf")
    plot_latency_distributions(valid, output_dir / "latency_distributions.pdf")
    plot_signing_regime(valid, output_dir / "signing_regime.pdf")
    plot_caching_benefit(valid, output_dir / "caching_benefit.pdf")
    campaign_dir = output_dir.parent
    plot_caching_ratio(valid, campaign_dir, output_dir / "caching_ratio.pdf")
    plot_signing_times(output_dir / "signing_times.pdf")
    plot_response_sizes(valid, output_dir / "response_sizes.pdf")
    plot_transport_breakdown(valid, output_dir / "transport_breakdown.pdf")
    plot_throughput_vs_signing_time(valid, output_dir / "throughput_vs_signing_time.pdf")
    plot_resource_usage(valid, output_dir / "resource_usage.pdf")
    plot_cache_effectiveness(valid, output_dir / "cache_effectiveness.pdf")
    plot_coalescence_vs_sigma(valid, output_dir / "coalescence_vs_sigma.pdf")
    plot_effective_signing_rate(valid, output_dir / "effective_signing_rate.pdf")
    # Saturation & response-size analysis (use all summaries for heatmap)
    all_finite = [s for s in summaries if np.isfinite(s.failure_rate)]
    plot_saturation_heatmap(all_finite, output_dir / "saturation_heatmap.pdf")
    plot_latency_vs_size(valid, output_dir / "latency_vs_size.pdf")
    plot_latency_bubble(valid, output_dir / "latency_bubble.pdf")
    # Network emulation (netem) plots — only generated when data is present
    if _has_netem_data(valid):
        plot_latency_vs_network_delay(valid, output_dir / "latency_vs_network_delay.pdf")
        plot_signing_impact_by_network(valid, output_dir / "signing_impact_by_network.pdf")
        plot_size_impact_by_network(valid, output_dir / "size_impact_by_network.pdf")
        plot_network_penalty_bars(valid, output_dir / "network_penalty_bars.pdf")
        plot_signing_regime_by_network(valid, output_dir / "signing_regime_by_network.pdf")
    if _has_bandwidth_data(valid):
        plot_latency_vs_bandwidth(valid, output_dir / "latency_vs_bandwidth.pdf")

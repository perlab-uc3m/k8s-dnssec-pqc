"""Stationary TTL checks and a reference-demand index for the final campaign.

The index uses elapsed signing time at the reference load, not measured CPU time.
It is descriptive and must not be interpreted as a utilization or latency bound.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .final import CAPACITY, COALESCENCE, _table
from .freshness import ideal_cache_rates


def offered_cache_model(row, lookups_per_query=2):
    """Uniform Poisson demand, including offered queries rejected under overload.

    Routing is fixed from the intended transport, never inferred from successful
    replies: doing that would make predicted demand fall when the server fails.
    """
    if (
        row.get("arrival_mode", "poisson") != "poisson"
        or row.get("popularity", "uniform") != "uniform"
    ):
        raise ValueError("This check requires uniform Poisson queries")
    n, replicas = row["names"], row["replicas"]
    q = row["offered"] / row["duration_s"]
    u = row["updates_acknowledged"] / row["duration_s"]
    reach = 1 - (1 - 1 / replicas) ** lookups_per_query
    rates = ideal_cache_rates(q * reach / n, u / n, row["response_cache_ttl"] or 0)
    return {
        "offered_rate": q,
        "acknowledged_update_rate": u,
        "signs_per_s": replicas * n * rates["signs_per_s"],
        "stale_fraction": rates["stale_fraction"],
    }


def export_model_checks(rows, output: Path):
    by = {c: [r for r in rows if r["cell_id"] == c] for c in sorted({r["cell_id"] for r in rows})}
    s0 = statistics.median(r["sign_wall_mean_ms"] for r in by["sphincs128s-u1"]) / 1000
    result = {"reference_signing_s": s0, "ttl": {}, "reference_demand": {}, "coalescence": {}}
    table = []
    for ttl in [0, 1, 5]:
        cell = "mldsa44-u8" + (f"-ttl{ttl}" if ttl else "")
        checked = []
        for row in by[cell]:
            model = offered_cache_model(row)
            checked.append(
                model
                | {
                    "repetition": row["repetition"],
                    "measured_signs_per_s": row["signs_per_s"],
                    "measured_stale_fraction": row["stale_fraction"],
                }
            )
        med = {k: statistics.median(r[k] for r in checked) for k in checked[0] if k != "repetition"}
        result["ttl"][cell] = {"runs": checked, "medians": med}
        table.append(
            [
                str(ttl),
                f"{med['signs_per_s']:.2f}",
                f"{med['measured_signs_per_s']:.2f}",
                f"{100 * med['stale_fraction']:.2f}",
                f"{100 * med['measured_stale_fraction']:.2f}",
            ]
        )
    _table(
        output / "table_ttl_model.tex",
        "Stationary response-cache prediction and measurement for ML-DSA-44 at eight configured updates/s. Predictions use each run's offered query rate and acknowledged update rate. Medians of five runs; stale fractions use all offered queries.",
        "tab:ttl_model",
        "@{}rrrrr@{}",
        [
            r"TTL (s) & \multicolumn{2}{c}{Signatures/s} & \multicolumn{2}{c}{Stale (\%)} \\",
            r" & Pred. & Meas. & Pred. & Meas. \\",
        ],
        table,
    )
    for cell, _ in CAPACITY:
        if not cell.startswith("sphincs"):
            continue
        checked = []
        for row in by[cell]:
            model = offered_cache_model(row)
            checked.append(
                {
                    "repetition": row["repetition"],
                    "index": model["signs_per_s"]
                    * s0
                    / (row["replicas"] * float(row["cpu_limit"])),
                    "answered_1s": row["answered_1s"],
                    "fresh_1s": row["fresh_on_time_1000ms"],
                    "predicted_signs_per_s": model["signs_per_s"],
                }
            )
        result["reference_demand"][cell] = {
            "runs": checked,
            "medians": {
                k: statistics.median(r[k] for r in checked) for k in checked[0] if k != "repetition"
            },
        }
    for cell, _ in COALESCENCE:
        if by[cell][0]["names"] != 1:
            continue
        checked = []
        for row in by[cell]:
            assert row["positive_unverified"] == row["offered"] == row["dns_transactions"]
            s = row["sign_wall_mean_ms"] / 1000
            a = row["offered"] / row["duration_s"]
            nominal = row["query_rate_configured"]
            checked.append(
                {
                    "repetition": row["repetition"],
                    "arrival_rate": a,
                    "predicted_realized": a * s / (1 + a * s),
                    "predicted_nominal": nominal * s / (1 + nominal * s),
                    "observed": row["coalescence_observed"],
                }
            )
        result["coalescence"][cell] = {
            "runs": checked,
            "medians": {
                k: statistics.median(r[k] for r in checked) for k in checked[0] if k != "repetition"
            },
        }
    result["client_rejections"] = {
        c: sum(r["client_rejected"] for r in group)
        for c, group in by.items()
        if any(r["client_rejected"] for r in group)
    }
    assert all(c.startswith("sphincs") for c in result["client_rejections"])
    (output / "model_checks.json").write_text(json.dumps(result, indent=2) + "\n")
    return result

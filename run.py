#!/usr/bin/env python3
"""PQC DNSSEC Kubernetes Benchmark CLI."""

import argparse
import logging
from pathlib import Path

from src.analyzer import analyze_all
from src.plotter import generate_all_plots
from src.runner import load_config, run_campaign

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("benchmark")


def main():
    parser = argparse.ArgumentParser(description="PQC DNSSEC Kubernetes Benchmark")
    parser.add_argument("--pilot", action="store_true", help="Run pilot only")
    parser.add_argument(
        "--short", action="store_true", help="Run short benchmark (all algos, fewer params)"
    )
    parser.add_argument(
        "--high-sigma",
        action="store_true",
        dest="high_sigma",
        help="Run high-Sigma campaign (TTL sweep, push toward saturation)",
    )
    parser.add_argument(
        "--sigma-sweep",
        action="store_true",
        dest="sigma_sweep",
        help="Run dense Sigma sweep (5 qr × 4 cr, phase-transition curves)",
    )
    parser.add_argument(
        "--simulated-sweep",
        action="store_true",
        dest="simulated_sweep",
        help="Run simulated signing delay sweep (UDP + TCP transport regimes)",
    )
    parser.add_argument(
        "--netem-sweep",
        action="store_true",
        dest="netem_sweep",
        help="Run network emulation sweep (tc-netem latency, reveals signature size impact)",
    )
    parser.add_argument(
        "--validation",
        action="store_true",
        help="Quick validation: all algos + simulated, minimal grid, 1 rep",
    )
    parser.add_argument(
        "--smoke", action="store_true", help="Minimal smoke test (1 PQC + 1 classical)"
    )
    parser.add_argument("--resume", action="store_true", help="Skip runs with existing results")
    parser.add_argument("--analyze", action="store_true", help="Analyze existing results")
    parser.add_argument("--plot", action="store_true", help="Generate plots from results")
    parser.add_argument("--config", type=Path, default=None, help="Config file path")
    parser.add_argument(
        "--campaign-id",
        type=str,
        default=None,
        dest="campaign_id",
        help="Campaign identifier (default: UTC timestamp). "
        "For --analyze/--plot, selects which campaign to process.",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    results_dir = Path(config["results"]["output_dir"])

    if args.analyze or args.plot:
        # Determine which campaign directory to analyze
        target = results_dir
        if args.campaign_id:
            target = results_dir / args.campaign_id
        else:
            # Auto-detect: use the most recent timestamped campaign subdirectory.
            # Timestamp dirs match YYYYMMDDTHHMMSSZ (20 chars, start with digit).
            # If none found, assume flat layout (old results) and use results_dir as-is.
            candidates = (
                sorted(
                    [d for d in results_dir.iterdir() if d.is_dir() and d.name[:1].isdigit()],
                    key=lambda p: p.name,
                )
                if results_dir.exists()
                else []
            )
            if candidates:
                target = candidates[-1]
                log.info("Auto-selected latest campaign: %s", target.name)

        log.info("Analyzing results in %s", target)
        summaries = analyze_all(target)
        log.info("Loaded %d run summaries", len(summaries))

        if args.plot:
            figures_dir = target / "figures"
            generate_all_plots(summaries, figures_dir)
            log.info("Figures saved to %s", figures_dir)
        else:
            for s in summaries:
                log.info(
                    "  %s cr=%.1f qr=%.0f | P50=%.1f P99=%.1f fail=%.3f Σ=%s",
                    s.algorithm,
                    s.churn_rate,
                    s.query_rate,
                    s.latency_p50,
                    s.latency_p99,
                    s.failure_rate,
                    f"{s.sigma:.3f}" if s.sigma is not None else "?",
                )
        return

    mode = (
        "smoke"
        if args.smoke
        else (
            "pilot"
            if args.pilot
            else (
                "short"
                if args.short
                else (
                    "high-sigma"
                    if args.high_sigma
                    else (
                        "sigma-sweep"
                        if args.sigma_sweep
                        else (
                            "simulated-sweep"
                            if args.simulated_sweep
                            else (
                                "netem-sweep"
                                if args.netem_sweep
                                else "validation"
                                if args.validation
                                else "full campaign"
                            )
                        )
                    )
                )
            )
        )
    )
    log.info("Starting %s%s", mode, " (resume)" if args.resume else "")
    campaign_id = run_campaign(
        config,
        pilot=args.pilot,
        short=args.short,
        smoke=args.smoke,
        high_sigma=args.high_sigma,
        sigma_sweep=args.sigma_sweep,
        simulated_sweep=args.simulated_sweep,
        netem_sweep=args.netem_sweep,
        validation=args.validation,
        resume=args.resume,
        campaign_id=args.campaign_id,
    )

    campaign_dir = Path(config["results"]["output_dir"]) / campaign_id
    log.info("Campaign complete. Analyzing results...")
    summaries = analyze_all(campaign_dir)
    figures_dir = campaign_dir / "figures"
    generate_all_plots(summaries, figures_dir)
    log.info("Done. Results in %s, figures in %s", campaign_dir, figures_dir)


if __name__ == "__main__":
    main()

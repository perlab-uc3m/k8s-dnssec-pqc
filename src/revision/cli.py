"""Single command local revision campaign runner."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import dns.message
import dns.query
import dns.rdatatype
import yaml

from revision.metrics import Metrics, cgroup_sample, validate_window
from revision.package import package, verify_package
from revision.paper import export_controls, export_paper
from revision.queries import run_load
from revision.report import report, summarize_run
from revision.workload import HeadlessWorkload, endpoint_address

ROOT = Path(__file__).resolve().parents[2]


def command(args: list[str], **kwargs) -> None:
    print("+", " ".join(args), flush=True)
    subprocess.run(args, check=True, **kwargs)


def config(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text())
    if doc.get("schema_version") != 1:
        raise ValueError("Unsupported campaign schema")
    required = (
        "campaign_id",
        "context",
        "namespace",
        "domain",
        "repetitions",
        "warmup_s",
        "measurement_s",
        "query_rate",
        "names",
        "cells",
    )
    for key in required:
        if key not in doc:
            raise ValueError(f"Missing campaign field: {key}")
    if not doc["cells"] or doc["repetitions"] < 1 or doc["names"] < 1:
        raise ValueError("At least one cell, repetition and name are required")
    ids = [cell["id"] for cell in doc["cells"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Cell ids must be unique")
    if doc.get("transport_policy", "fresh_fallback") not in (
        "fresh_fallback",
        "fresh_tcp",
        "reused_tcp",
    ):
        raise ValueError("Unknown transport policy")
    return doc


def artifact_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def host_manifest() -> dict:
    manifest = {
        "platform": platform.platform(),
        "python": sys.version,
        "utc_start": time.time(),
        "cpu_count": os.cpu_count(),
    }
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        manifest["cpu_model"] = next(
            (
                line.partition(":")[2].strip()
                for line in cpuinfo.read_text().splitlines()
                if line.startswith("model name")
            ),
            None,
        )
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        manifest["mem_total_kib"] = next(
            (
                int(line.partition(":")[2].split()[0])
                for line in meminfo.read_text().splitlines()
                if line.startswith("MemTotal:")
            ),
            None,
        )
    for name in ("coredns-pqc", "verify-dns", "keygen", "keygen-ed", "image-id.txt"):
        path = ROOT / "build" / name
        if path.is_file():
            manifest[name] = artifact_hash(path)
    for name in ("dns", "coredns", "dnssec_pqc_plugin", "liboqs"):
        path = ROOT / "build" / name
        if (path / ".git").exists():
            manifest[name + "_commit"] = subprocess.check_output(
                ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
            ).strip()
    manifest["dns_patch_sha256"] = artifact_hash(ROOT / "patches/dns-pqc-verify.patch")
    manifest["plugin_patch_sha256"] = artifact_hash(ROOT / "patches/plugin-signing-failure.patch")
    manifest["go_version"] = subprocess.check_output(["go", "version"], text=True).strip()
    governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    if governor.is_file():
        manifest["cpu_governor"] = governor.read_text().strip()
    return manifest


def readiness(host: str, port: int, expected: dict[str, str]) -> dict:
    deadline = time.monotonic() + 45
    last = {}
    while time.monotonic() < deadline:
        result = {}
        for name in expected:
            try:
                q = dns.message.make_query(
                    name, dns.rdatatype.A, want_dnssec=True, use_edns=0, payload=1232
                )
                answer = dns.query.udp(q, host, port=port, timeout=2, raise_on_truncation=False)
                if answer.flags & 0x0200:
                    answer = dns.query.tcp(q, host, port=30054, timeout=3)
                addresses = [
                    r.to_text()
                    for rrset in answer.answer
                    if rrset.rdtype == dns.rdatatype.A
                    for r in rrset
                ]
                result[name] = addresses
            except Exception as exc:
                result[name] = [f"error: {exc}"]
        last = result
        if all(result[name] == [wanted] for name, wanted in expected.items()):
            return result
        time.sleep(0.5)
    raise RuntimeError(f"Readiness did not observe expected EndpointSlices: {last}")


async def run_one(doc: dict, cell: dict, repetition: int, attempt: Path, kubectl: str) -> dict:
    doc = {**doc, **cell}
    attempt.mkdir(parents=True)
    environment = dict(os.environ)
    environment.update(
        {
            "BENCH_KUBECTL": kubectl,
            "BENCH_CONTEXT": doc["context"],
            "KUBERNETES_TTL": str(doc.get("record_ttl", 5)),
            "COREDNS_CPU_LIMIT": str(doc.get("cpu_limit", "1")),
            "COREDNS_MEMORY_LIMIT": str(doc.get("memory_limit", "512Mi")),
            "COREDNS_REPLICAS": str(doc.get("replicas", 1)),
            "COREDNS_IMAGE": "coredns-pqc:revision",
        }
    )
    (attempt / "host.json").write_text(json.dumps(host_manifest(), indent=2) + "\n")
    run_config = {
        "schema_version": 1,
        "cell_id": cell["id"],
        "repetition": repetition,
        "algorithm": cell["algorithm"],
        "algorithm_id": cell["algorithm_id"],
        "workload": "fixed_headless_endpoint_updates",
        "transport_policy": doc.get("transport_policy", "fresh_fallback"),
        "arrival_mode": doc.get("arrival_mode", "poisson"),
        "popularity": doc.get("popularity", "uniform"),
        "replicas": doc.get("replicas", 1),
        "names": doc["names"],
        "query_rate": doc["query_rate"],
        "seed_base": doc.get("seed", 1),
        "update_rate": doc.get("update_rate", 0),
        "measurement_s": doc["measurement_s"],
        "warmup_s": doc["warmup_s"],
        "signature_cache_capacity": doc.get("sig_cache_cap", 1024),
        "response_cache_ttl": doc.get("response_cache_ttl", 0),
        "kubernetes_ttl": doc.get("record_ttl", 5),
        "cpu_limit": doc.get("cpu_limit", "1"),
        "memory_limit": doc.get("memory_limit", "512Mi"),
    }
    (attempt / "config.json").write_text(json.dumps(run_config, indent=2) + "\n")
    fixture = HeadlessWorkload(
        doc["namespace"],
        doc["domain"],
        doc["names"],
        doc["context"],
        doc.get("seed", 1) + repetition,
    )
    metrics = Metrics(kubectl, doc["context"], attempt)
    update_task = None
    try:
        await asyncio.to_thread(fixture.setup)
        command(
            [
                str(ROOT / "scripts/deploy_coredns.sh"),
                cell["algorithm"],
                str(cell["algorithm_id"]),
                str(doc.get("response_cache_ttl", 0)),
                cell["algorithm_type"],
                str(doc.get("sig_cache_cap", 1024)),
            ],
            env=environment,
        )
        key_files = list((ROOT / "build/keys").glob("*.key"))
        if cell["algorithm_type"] != "baseline":
            if len(key_files) != 1:
                raise RuntimeError(f"Expected one DNSKEY, found {len(key_files)}")
            shutil.copyfile(key_files[0], attempt / "trust_anchor.key")
        ready = await asyncio.to_thread(
            readiness,
            "127.0.0.1",
            30053,
            {fixture.fqdn(i): endpoint_address(i, 0) for i in range(fixture.count)},
        )
        (attempt / "readiness.json").write_text(json.dumps(ready, indent=2) + "\n")
        warmup = await run_load(
            None,
            "127.0.0.1",
            30053,
            30054,
            list(ready),
            doc["query_rate"],
            doc["warmup_s"],
            doc.get("seed", 1) + repetition + 5000,
            policy=doc.get("transport_policy", "fresh_fallback"),
        )
        (attempt / "warmup.json").write_text(json.dumps(warmup, indent=2) + "\n")
        metrics.start(time.monotonic())
        await metrics.sample("measurement_start")
        await cgroup_sample(metrics, "measurement_start")
        anchor = time.monotonic()
        utc_anchor = time.time()
        metrics.anchor = anchor
        update_task = asyncio.create_task(
            fixture.changes(attempt, anchor, doc["measurement_s"], doc.get("update_rate", 0))
        )
        metrics.poll_task = asyncio.create_task(metrics.poll("measurement", 1.0))

        async def window_end() -> None:
            metrics.poll_task.cancel()
            await metrics.poll_task
            await metrics.sample("measurement_end")
            await cgroup_sample(metrics, "measurement_end")

        load = await run_load(
            attempt,
            "127.0.0.1",
            30053,
            30054,
            list(ready),
            doc["query_rate"],
            doc["measurement_s"],
            doc.get("seed", 1) + repetition,
            policy=doc.get("transport_policy", "fresh_fallback"),
            at_window_end=window_end,
            anchor=anchor,
            utc_anchor=utc_anchor,
            timeout_s=doc.get("timeout_s", 5.0),
            max_inflight=doc.get("max_inflight", 256),
            tcp_connections=doc.get("tcp_connections", 16),
            arrival_mode=doc.get("arrival_mode", "poisson"),
            popularity=doc.get("popularity", "uniform"),
        )
        (attempt / "load.json").write_text(json.dumps(load, indent=2) + "\n")
        update_result = await update_task
        (attempt / "update_summary.json").write_text(json.dumps(update_result, indent=2) + "\n")
        await metrics.sample("post_drain")
        metrics.close()
        quality = validate_window(
            attempt / "metrics.jsonl", doc["measurement_s"], len(metrics.pods)
        )
        (attempt / "metrics_quality.json").write_text(json.dumps(quality, indent=2) + "\n")
        if cell["algorithm_type"] != "baseline":
            command(
                [
                    str(ROOT / "build/verify-dns"),
                    "-run-dir",
                    str(attempt),
                    "-key",
                    str(attempt / "trust_anchor.key"),
                ]
            )
        summary = summarize_run(attempt)
        (attempt / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        (attempt / "COMPLETE").write_text(str(time.time()) + "\n")
        return summary
    except BaseException as exc:
        (attempt / "FAILED").write_text(f"{type(exc).__name__}: {exc}\n")
        raise
    finally:
        if metrics.poll_task is not None and not metrics.poll_task.done():
            metrics.poll_task.cancel()
            try:
                await metrics.poll_task
            except asyncio.CancelledError:
                pass
        if update_task is not None and not update_task.done():
            update_task.cancel()
            try:
                await update_task
            except asyncio.CancelledError:
                pass
        metrics.close()
        await asyncio.to_thread(fixture.cleanup)


async def reproduce(path: Path, output: Path, *, build: bool, setup: bool) -> None:
    doc = config(path)
    output.mkdir(parents=True, exist_ok=True)
    frozen = output / "campaign.yaml"
    if frozen.exists() and frozen.read_bytes() != path.read_bytes():
        raise ValueError(f"Campaign configuration changed since first run: {frozen}")
    if not frozen.exists():
        shutil.copyfile(path, frozen)
    (output / "campaign_sha256.txt").write_text(artifact_hash(frozen) + "\n")
    if build:
        command([str(ROOT / "scripts/build_revision.sh")])
    if setup:
        command([str(ROOT / "scripts/setup_revision_cluster.sh")])
    kubectl = str(ROOT / "build/tools/kubectl")
    command([kubectl, "--context", doc["context"], "cluster-info"])
    for repetition in range(1, doc["repetitions"] + 1):
        for cell in doc["cells"]:
            base = output / "runs" / cell["id"] / f"rep-{repetition:02d}"
            if list(base.glob("attempt-*/COMPLETE")):
                print(f"Skipping complete {cell['id']} repetition {repetition}", flush=True)
                continue
            attempt_no = 1 + len(list(base.glob("attempt-*")))
            run_dir = base / f"attempt-{attempt_no:02d}"
            print(f"Run {cell['id']}, repetition {repetition}, attempt {attempt_no}", flush=True)
            await run_one(doc, cell, repetition, run_dir, kubectl)
    report(output)
    print(f"Campaign report: {output / 'report.md'}", flush=True)


def doctor() -> None:
    import shutil as _shutil
    import socket
    from pathlib import Path as _Path

    mem = {}
    for line in _Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable", "SwapFree"):
            mem[key] = int(rest.split()[0]) * 1024
    tools = {}
    for name in ("go", "cmake", "ninja", "gcc", "docker", "curl", "sha256sum"):
        tools[name] = _shutil.which(name)
    ports = {}
    for port, kind in (
        (30053, socket.SOCK_DGRAM),
        (30054, socket.SOCK_STREAM),
        (30153, socket.SOCK_STREAM),
    ):
        sock = socket.socket(socket.AF_INET, kind)
        try:
            sock.bind(("127.0.0.1", port))
            ports[str(port)] = "available"
        except OSError:
            ports[str(port)] = "in use (expected if Kind already exists)"
        finally:
            sock.close()
    print(
        json.dumps(
            {
                "cpu_count": os.cpu_count(),
                "memory_bytes": mem,
                "disk_free_bytes": _shutil.disk_usage(ROOT).free,
                "tools": tools,
                "ports": ports,
                "kind_version": subprocess.check_output(
                    [str(ROOT / "build/tools/kind"), "version"], text=True
                ).strip(),
                "kubectl_version": subprocess.check_output(
                    [str(ROOT / "build/tools/kubectl"), "version", "--client"], text=True
                ).strip(),
                "go_version": subprocess.check_output(["go", "version"], text=True).strip(),
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("doctor", help="Report local resource and tool preflight")
    sub.add_parser("cleanup", help="Delete only the named revision Kind cluster")
    p = sub.add_parser("reproduce", help="Build, run locally, verify, and plot")
    p.add_argument("config", type=Path, nargs="?", default=ROOT / "config/revision-smoke.yaml")
    p.add_argument("--output", type=Path)
    p.add_argument("--no-build", action="store_true")
    p.add_argument("--no-setup", action="store_true")
    p = sub.add_parser("report", help="Regenerate tables and figures offline")
    p.add_argument("campaign", type=Path)
    p = sub.add_parser("export-paper", help="Generate manuscript table and figures from raw runs")
    p.add_argument("campaign", type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--ttl", type=Path, help="Response-cache control campaign")
    p.add_argument("--stress", type=Path, help="Matched CPU budget campaign")
    p = sub.add_parser("package", help="Archive raw campaigns with per-file SHA256 manifest")
    p.add_argument("campaigns", nargs="+", type=Path)
    p.add_argument("--output", required=True, type=Path)
    p = sub.add_parser("verify-package", help="Verify every file in a campaign archive")
    p.add_argument("archive", type=Path)
    p.add_argument("manifest", type=Path)
    p = sub.add_parser("plan", help="Check campaign configuration and time estimate")
    p.add_argument("config", type=Path, nargs="?", default=ROOT / "config/revision-smoke.yaml")
    args = parser.parse_args()
    if args.action == "doctor":
        doctor()
    elif args.action == "cleanup":
        command(
            [
                str(ROOT / "build/tools/kind"),
                "delete",
                "cluster",
                "--name",
                os.environ.get("BENCH_CLUSTER_NAME", "pqc-dnssec-revision"),
            ]
        )
    elif args.action == "report":
        report(args.campaign)
    elif args.action == "export-paper":
        if bool(args.ttl) != bool(args.stress):
            raise ValueError("--ttl and --stress must be supplied together")
        export_paper(args.campaign, args.output)
        if args.ttl:
            export_controls(args.ttl, args.stress, args.output)
    elif args.action == "package":
        print(package(args.campaigns, args.output))
    elif args.action == "verify-package":
        print(f"Verified {verify_package(args.archive, args.manifest)} files")
    elif args.action == "plan":
        doc = config(args.config)
        seconds = doc["repetitions"] * len(doc["cells"]) * (doc["warmup_s"] + doc["measurement_s"])
        print(
            json.dumps(
                {
                    "cells": len(doc["cells"]),
                    "repetitions": doc["repetitions"],
                    "minimum_measurement_and_warmup_s": seconds,
                    "additional_time": "build, deploy, readiness, verification, plotting",
                },
                indent=2,
            )
        )
    else:
        doc = config(args.config)
        output = args.output or ROOT / "results/revision" / doc["campaign_id"]
        asyncio.run(
            reproduce(args.config, output, build=not args.no_build, setup=not args.no_setup)
        )


if __name__ == "__main__":
    main()

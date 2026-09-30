"""Single command local revision campaign runner."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import time
from pathlib import Path

import dns.message
import dns.query
import dns.rdatatype
import yaml

from .metrics import Metrics, cgroup_sample, validate_window
from .package import package, verify_package
from .paper import export_controls, export_paper
from .queries import ReusedTCP, run_load
from .report import report, summarize_run
from .resources import HostMonitor, snapshot
from .workload import HeadlessWorkload, endpoint_address

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
    if doc.get("cell_order", "fixed") not in ("fixed", "randomized"):
        raise ValueError("cell_order must be fixed or randomized")
    for cell in doc["cells"]:
        values = {**doc, **cell}
        if not 1 <= values["names"] <= 250:
            raise ValueError("names must be between 1 and 250")
        if values["query_rate"] <= 0 or values["measurement_s"] <= 0 or values["warmup_s"] <= 0:
            raise ValueError("positive rates and run windows required")
        if values.get("sampling_interval_s", 1) < 0:
            raise ValueError("sampling_interval_s cannot be negative")
        if values.get("update_rate", 0) < 0 or values.get("response_cache_ttl", 0) < 0:
            raise ValueError("update rate and cache TTL cannot be negative")
        if values.get("require_ownership_fix") and values.get("record_ttl", 5) < values.get(
            "response_cache_ttl", 0
        ):
            raise ValueError(
                "record_ttl must cover the response-cache ceiling in the freshness design"
            )
    return doc


def cell_schedule(doc: dict) -> list[dict]:
    schedule = []
    for repetition in range(1, doc["repetitions"] + 1):
        cells = list(doc["cells"])
        if doc.get("cell_order", "fixed") == "randomized":
            random.Random(doc.get("seed", 1) ^ (repetition * 0x68BC21EB)).shuffle(cells)
        schedule.extend({"repetition": repetition, "cell_id": cell["id"]} for cell in cells)
    return schedule


def artifact_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for block in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validated_build_manifest() -> dict:
    path = ROOT / "build/source-manifest.json"
    if not path.exists():
        raise ValueError("Missing build source manifest; run ./bench reproduce without --no-build")
    record = json.loads(path.read_text())
    for name, expected in record["inputs"].items():
        if artifact_hash(ROOT / name) != expected:
            raise ValueError(f"Build input changed: {name}; rebuild before measurement")
    if artifact_hash(ROOT / "build/coredns-pqc") != record["binary_sha256"]:
        raise ValueError("CoreDNS binary differs from its build manifest")
    return record


def freeze_acquisition(output: Path, record: dict) -> None:
    """Prevent resumed campaigns from mixing measurement implementations."""
    path = output / "acquisition_manifest.json"
    if path.exists():
        if json.loads(path.read_text()) != record:
            raise ValueError(
                "Campaign build or acquisition source changed; use a new output directory"
            )
    elif list(output.glob("runs/*/rep-*/attempt-*/COMPLETE")):
        raise ValueError(
            "Archived campaign has no build guard; use report or a new output directory"
        )
    else:
        path.write_text(json.dumps(record, indent=2) + "\n")


def host_manifest() -> dict:
    manifest = {
        "build": validated_build_manifest(),
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
    manifest["harness_sha256"] = {
        str(path.relative_to(ROOT)): artifact_hash(path)
        for path in sorted((ROOT / "src/revision").glob("*.py"))
    }
    manifest["timing_schema"] = 2
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
    manifest = host_manifest()
    if doc.get("require_ownership_fix") and not manifest["build"].get("signature_ownership_fix"):
        raise ValueError("This campaign requires the signature ownership fix")
    (attempt / "host.json").write_text(json.dumps(manifest, indent=2) + "\n")
    run_config = {
        "schema_version": 1,
        "study": doc.get("study", "revision_sensitivity"),
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
        "sampling_interval_s": doc.get("sampling_interval_s", 1.0),
        "dynamic_warmup": doc.get("dynamic_warmup", False),
        "query_seed": doc.get("seed", 1) + repetition,
        "update_seed": (doc.get("seed", 1) + repetition) ^ 0xA27149E3,
        "max_inflight": doc.get("max_inflight", 256),
        "tcp_connections": doc.get("tcp_connections", 16),
        "timeout_s": doc.get("timeout_s", 5.0),
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
    update_task = warmup_update_task = host_task = None
    monitor = HostMonitor(attempt / "host_metrics.jsonl")
    client_pool = (
        ReusedTCP("127.0.0.1", 30054, doc.get("tcp_connections", 16))
        if doc.get("transport_policy") == "reused_tcp"
        else None
    )
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
        (attempt / "initial_state.json").write_text(
            json.dumps(
                {name: {"address": values[0], "version": 0} for name, values in ready.items()},
                indent=2,
            )
            + "\n"
        )
        metrics.start(time.monotonic(), expected_replicas=doc.get("replicas", 1))
        warmup_anchor = time.monotonic()
        if doc.get("dynamic_warmup", False):
            warmup_dir = attempt / "warmup"
            warmup_dir.mkdir()
            warmup_update_task = asyncio.create_task(
                fixture.changes(
                    warmup_dir,
                    warmup_anchor,
                    doc["warmup_s"],
                    doc.get("update_rate", 0),
                    seed=run_config["update_seed"] ^ 0x35718DA7,
                )
            )
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
            anchor=warmup_anchor,
            timeout_s=doc.get("timeout_s", 5.0),
            max_inflight=doc.get("max_inflight", 256),
            tcp_connections=doc.get("tcp_connections", 16),
            arrival_mode=doc.get("arrival_mode", "poisson"),
            popularity=doc.get("popularity", "uniform"),
            tcp_pool=client_pool,
        )
        if warmup_update_task is not None:
            updates = await warmup_update_task
            (attempt / "warmup/update_summary.json").write_text(
                json.dumps(updates, indent=2) + "\n"
            )
            if updates["errors"] or updates["deadline_missed"]:
                raise RuntimeError(
                    "Warmup update schedule was not achieved; inspect warmup artifacts"
                )
        (attempt / "warmup.json").write_text(json.dumps(warmup, indent=2) + "\n")
        await cgroup_sample(metrics, "measurement_start")
        await metrics.sample("measurement_start")
        anchor = time.monotonic()
        utc_anchor = time.time()
        (attempt / "warmup_timing.json").write_text(
            json.dumps({"warmup_to_measurement_offset_s": warmup_anchor - anchor}, indent=2) + "\n"
        )
        monitor.sample("measurement_start")
        interval = doc.get("sampling_interval_s", 1.0)
        host_task = asyncio.create_task(monitor.poll(interval)) if interval > 0 else None
        update_task = asyncio.create_task(
            fixture.changes(
                attempt,
                anchor,
                doc["measurement_s"],
                doc.get("update_rate", 0),
                seed=run_config["update_seed"],
            )
        )
        metrics.poll_task = (
            asyncio.create_task(metrics.poll("measurement", interval)) if interval > 0 else None
        )

        async def window_end() -> None:
            if host_task is not None:
                if host_task.done() and not host_task.cancelled():
                    host_task.result()
                host_task.cancel()
                await asyncio.gather(host_task, return_exceptions=True)
            monitor.sample("measurement_end")
            if metrics.poll_task is not None:
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
            tcp_pool=client_pool,
        )
        (attempt / "load.json").write_text(json.dumps(load, indent=2) + "\n")
        update_result = await update_task
        (attempt / "update_summary.json").write_text(json.dumps(update_result, indent=2) + "\n")
        await metrics.sample("post_drain")
        metrics.close()
        try:
            quality = {
                "valid": True,
                "pods": validate_window(
                    attempt / "metrics.jsonl", doc["measurement_s"], len(metrics.pods), interval
                ),
            }
        except (RuntimeError, ValueError, OSError) as exc:
            quality = {"valid": False, "reason": str(exc)}
        try:
            status = await asyncio.to_thread(
                subprocess.check_output,
                [
                    kubectl,
                    "--context",
                    doc["context"],
                    "-n",
                    "kube-system",
                    "get",
                    "pods",
                    "-l",
                    "app=coredns-pqc",
                    "-o",
                    "json",
                ],
                text=True,
                timeout=15,
            )
            (attempt / "pod_status.json").write_text(status)
        except (OSError, subprocess.SubprocessError) as exc:
            (attempt / "pod_status_error.txt").write_text(str(exc) + "\n")
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
        for task in (warmup_update_task, host_task):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if client_pool is not None:
            await client_pool.close()
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
    memory = dict(
        (line.partition(":")[0], int(line.partition(":")[2].split()[0]))
        for line in Path("/proc/meminfo").read_text().splitlines()
    )
    required_kib = (5 if setup else 1) * 1024 * 1024
    if memory["MemAvailable"] < required_kib:
        raise RuntimeError(
            f"Insufficient host memory headroom: {memory['MemAvailable'] / 1024**2:.2f} GiB available; "
            f"{required_kib / 1024**2:g} GiB required for this step. No campaign parameters were changed."
        )
    output.mkdir(parents=True, exist_ok=True)
    frozen = output / "campaign.yaml"
    if frozen.exists() and frozen.read_bytes() != path.read_bytes():
        raise ValueError(f"Campaign configuration changed since first run: {frozen}")
    if not frozen.exists():
        shutil.copyfile(path, frozen)
    (output / "campaign_sha256.txt").write_text(artifact_hash(frozen) + "\n")
    if build:
        command([str(ROOT / "scripts/build_revision.sh")])
    build_record = validated_build_manifest()
    actual_image = subprocess.check_output(
        ["docker", "image", "inspect", "coredns-pqc:revision", "--format", "{{.Id}}"], text=True
    ).strip()
    if actual_image != build_record["image_id"]:
        raise ValueError(
            "CoreDNS image tag differs from the build manifest; rebuild before measurement"
        )
    acquisition_files = [
        ROOT / "src/revision" / name
        for name in ("cli.py", "metrics.py", "queries.py", "workload.py", "resources.py")
    ]
    acquisition_files.append(ROOT / "scripts/deploy_coredns.sh")
    freeze_acquisition(
        output,
        {
            "build": build_record,
            "acquisition": {str(p.relative_to(ROOT)): artifact_hash(p) for p in acquisition_files},
        },
    )
    if setup:
        command([str(ROOT / "scripts/setup_revision_cluster.sh")])
    kubectl = str(ROOT / "build/tools/kubectl")
    command([kubectl, "--context", doc["context"], "cluster-info"])
    schedule = cell_schedule(doc)
    (output / "schedule.json").write_text(json.dumps(schedule, indent=2) + "\n")
    cells = {cell["id"]: cell for cell in doc["cells"]}
    for item in schedule:
        repetition, cell = item["repetition"], cells[item["cell_id"]]
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
                "resource_snapshot": snapshot(),
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
    p.add_argument("config", type=Path, nargs="?", default=ROOT / "config/freshness-smoke.yaml")
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
    p.add_argument("config", type=Path, nargs="?", default=ROOT / "config/freshness-smoke.yaml")
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
        seconds = doc["repetitions"] * sum(
            cell.get("warmup_s", doc["warmup_s"]) + cell.get("measurement_s", doc["measurement_s"])
            for cell in doc["cells"]
        )
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

"""Per-pod Prometheus snapshots without a metrics-server dependency."""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

_FORWARD = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+) -> 9153")
_KEEP = (
    "process_cpu_seconds_total",
    "process_start_time_seconds",
    "process_resident_memory_bytes",
    "go_goroutines",
    "go_memstats_",
    "coredns_dnssec_pqc_",
    "coredns_cache_",
    "coredns_dns_requests_total",
    "coredns_dns_responses_total",
    "coredns_dns_request_duration_seconds_",
)


def parse_prometheus(body: str) -> dict[str, float]:
    series: dict[str, float] = {}
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        key = fields[0]
        if not key.startswith(_KEEP):
            continue
        try:
            series[key] = float(fields[1])
        except ValueError:
            continue
    return series


def _fetch(url: str) -> dict[str, float]:
    with urllib.request.urlopen(url, timeout=3) as response:
        return parse_prometheus(response.read().decode("utf-8"))


def _kubectl_json(kubectl: str, context: str, *args: str) -> dict:
    return json.loads(
        subprocess.check_output(
            [kubectl, "--context", context, *args],
            text=True,
            timeout=15,
        )
    )


class PodForward:
    def __init__(self, kubectl: str, context: str, namespace: str, pod: str):
        self.kubectl, self.context = kubectl, context
        self.namespace, self.pod = namespace, pod
        self.process: subprocess.Popen | None = None
        self.url: str | None = None
        self.ready = threading.Event()
        self.lines: deque[str] = deque(maxlen=40)
        self.reader: threading.Thread | None = None

    def __enter__(self):
        self.process = subprocess.Popen(
            [
                self.kubectl,
                "--context",
                self.context,
                "-n",
                self.namespace,
                "port-forward",
                "--address",
                "127.0.0.1",
                f"pod/{self.pod}",
                "0:9153",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        def drain() -> None:
            assert self.process is not None and self.process.stdout is not None
            for line in self.process.stdout:
                self.lines.append(line.rstrip())
                match = _FORWARD.search(line)
                if match:
                    self.url = f"http://127.0.0.1:{match.group(1)}/metrics"
                    self.ready.set()

        self.reader = threading.Thread(target=drain, daemon=True)
        self.reader.start()
        if self.ready.wait(15):
            return self
        message = "; ".join(self.lines)
        self.__exit__(None, None, None)
        raise RuntimeError(f"kubectl port-forward failed for {self.pod}: {message}")

    def __exit__(self, *_):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            if self.process.stdout is not None:
                self.process.stdout.close()
            self.process = None
        if self.reader is not None:
            self.reader.join(timeout=1)
            self.reader = None


class Metrics:
    def __init__(self, kubectl: str, context: str, output: Path):
        self.kubectl, self.context, self.output = kubectl, context, output
        self.forwards: list[PodForward] = []
        self.pods: list[dict] = []
        self.file = None
        self.anchor = 0.0
        self.poll_task: asyncio.Task | None = None

    def start(self, anchor: float, expected_replicas: int = 1) -> None:
        self.anchor = anchor
        doc = _kubectl_json(
            self.kubectl,
            self.context,
            "-n",
            "kube-system",
            "get",
            "pods",
            "-l",
            "app=coredns-pqc",
            "-o",
            "json",
        )
        self.pods = [
            {
                "name": item["metadata"]["name"],
                "uid": item["metadata"]["uid"],
                "node": item["spec"].get("nodeName"),
                "phase": item["status"].get("phase"),
            }
            for item in doc["items"]
            if item["metadata"].get("deletionTimestamp") is None
            and any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in item["status"].get("conditions", [])
            )
        ]
        if len(self.pods) != expected_replicas or any(p["phase"] != "Running" for p in self.pods):
            raise RuntimeError(f"CoreDNS pods unavailable: {self.pods}")
        try:
            for pod in self.pods:
                forward = PodForward(
                    self.kubectl,
                    self.context,
                    "kube-system",
                    pod["name"],
                )
                self.forwards.append(forward.__enter__())
            self.file = (self.output / "metrics.jsonl").open("w")
        except BaseException:
            self.close()
            raise

    async def sample(self, phase: str) -> list[dict]:
        samples = []
        for pod, forward in zip(self.pods, self.forwards, strict=True):
            try:
                series = await asyncio.to_thread(_fetch, forward.url)
                sample = {
                    "monotonic_s": time.monotonic(),
                    "t_s": time.monotonic() - self.anchor,
                    "phase": phase,
                    "pod": pod["name"],
                    "uid": pod["uid"],
                    "node": pod["node"],
                    "series": series,
                }
            except Exception as exc:
                sample = {
                    "monotonic_s": time.monotonic(),
                    "t_s": time.monotonic() - self.anchor,
                    "phase": phase,
                    "pod": pod["name"],
                    "uid": pod["uid"],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            self.file.write(json.dumps(sample, separators=(",", ":")) + "\n")
            samples.append(sample)
        self.file.flush()
        return samples

    async def poll(self, phase: str, interval_s: float = 1.0) -> None:
        try:
            while True:
                await self.sample(phase)
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return

    def close(self) -> None:
        if self.file is not None:
            self.file.close()
            self.file = None
        for forward in reversed(self.forwards):
            forward.__exit__(None, None, None)
        self.forwards.clear()


def measurement_window(samples: list[dict]) -> tuple[dict, dict, str]:
    """Use one clock domain; legacy start snapshots preceded a clock reset.

    Legacy runs retain periodic samples in the load clock domain. Their first
    complete periodic scrape and the end snapshot provide an observed interval
    without guessing the missing clock offset.
    """
    ends = [row for row in samples if row["phase"] == "measurement_end"]
    if len(ends) != 1:
        raise ValueError("Expected exactly one measurement end snapshot")
    end = ends[0]
    if "monotonic_s" in end:
        starts = [row for row in samples if row["phase"] == "measurement_start"]
        if len(starts) != 1 or "monotonic_s" not in starts[0]:
            raise ValueError("Missing absolute clock boundary")
        return starts[0], end, "absolute_monotonic_boundaries"
    first = next(
        (
            row
            for row in samples
            if row["phase"] == "measurement"
            and "process_cpu_seconds_total" in row.get("series", {})
        ),
        None,
    )
    if first is None:
        raise ValueError("Legacy run has no periodic sample in the load clock domain")
    return first, end, "legacy_first_periodic_to_end"


def window_elapsed(first: dict, last: dict) -> float:
    field = "monotonic_s" if "monotonic_s" in last else "t_s"
    return last[field] - first[field]


def validate_window(
    path: Path, duration_s: float, expected_pods: int, interval_s: float = 1.0
) -> dict:
    """Reject runs whose CPU window or periodic samples are incomplete."""
    rows = [json.loads(line) for line in path.open()]
    by_uid: dict[str, list[dict]] = {}
    for row in rows:
        by_uid.setdefault(row["uid"], []).append(row)
    if len(by_uid) != expected_pods:
        raise RuntimeError(f"Metrics expected {expected_pods} pods, found {len(by_uid)}")
    result = {}
    for uid, samples in by_uid.items():
        starts = [x for x in samples if x["phase"] == "measurement_start"]
        ends = [x for x in samples if x["phase"] == "measurement_end"]
        period = [x for x in samples if x["phase"] == "measurement"]
        if len(starts) != 1 or len(ends) != 1:
            raise RuntimeError(f"Missing metric boundary for pod {uid}")
        first, last, source = measurement_window(samples)
        for x in (first, last):
            series = x.get("series", {})
            if (
                "process_cpu_seconds_total" not in series
                or "process_resident_memory_bytes" not in series
            ):
                raise RuntimeError(
                    f"Missing process metrics at {x['phase']} for pod {uid}: {x.get('error')}"
                )
        if (
            last["series"]["process_cpu_seconds_total"]
            < first["series"]["process_cpu_seconds_total"]
        ):
            raise RuntimeError(f"CPU counter decreased for pod {uid}")
        if first["series"].get("process_start_time_seconds") != last["series"].get(
            "process_start_time_seconds"
        ):
            raise RuntimeError(f"CoreDNS process restarted during measurement for pod {uid}")
        if window_elapsed(first, last) < duration_s * 0.9:
            raise RuntimeError(f"Short metrics window for pod {uid}")
        good = sum("series" in x and "process_cpu_seconds_total" in x["series"] for x in period)
        if interval_s > 0 and good < duration_s * 0.8 / interval_s:
            raise RuntimeError(f"Only {good} valid periodic samples for pod {uid} in {duration_s}s")
        result[uid] = {
            "periodic_good": good,
            "periodic_total": len(period),
            "cpu_seconds": last["series"]["process_cpu_seconds_total"]
            - first["series"]["process_cpu_seconds_total"],
            "elapsed_s": window_elapsed(first, last),
            "window_source": source,
        }
    return result


def _cgroup(kubectl: str, context: str, pod: str) -> dict:
    script = (
        "for f in cpu.max cpu.stat memory.current memory.peak memory.events; "
        'do echo "==${f}=="; cat "/sys/fs/cgroup/${f}"; done'
    )
    output = subprocess.check_output(
        [kubectl, "--context", context, "-n", "kube-system", "exec", pod, "--", "sh", "-c", script],
        text=True,
        timeout=8,
    )
    result: dict[str, dict | str] = {}
    section = None
    for line in output.splitlines():
        if line.startswith("==") and line.endswith("=="):
            section = line.strip("=")
            result[section] = {}
        elif section == "cpu.max":
            result[section] = line
        elif section in ("memory.current", "memory.peak"):
            result[section] = int(line)
        elif section is not None:
            parts = line.split()
            if len(parts) == 2:
                result[section][parts[0]] = int(parts[1])
    return result


async def cgroup_sample(metrics: Metrics, phase: str) -> list[dict]:
    """One low-frequency cgroup snapshot per benchmark pod."""
    rows = []
    for pod in metrics.pods:
        try:
            counters = await asyncio.to_thread(
                _cgroup, metrics.kubectl, metrics.context, pod["name"]
            )
            row = {
                "phase": phase,
                "pod": pod["name"],
                "uid": pod["uid"],
                "t_s": time.monotonic() - metrics.anchor,
                "cgroup": counters,
            }
        except Exception as exc:
            row = {
                "phase": phase,
                "pod": pod["name"],
                "uid": pod["uid"],
                "t_s": time.monotonic() - metrics.anchor,
                "error": f"{type(exc).__name__}: {exc}",
            }
        rows.append(row)
    with (metrics.output / "cgroup.jsonl").open("a") as out:
        for row in rows:
            out.write(json.dumps(row) + "\n")
    return rows

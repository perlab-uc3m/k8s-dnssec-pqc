"""Low frequency host and client measurements from procfs."""

from __future__ import annotations

import asyncio
import json
import os
import resource
import time
from pathlib import Path


def snapshot() -> dict:
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        name, _, rest = line.partition(":")
        if name in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
            memory[name] = int(rest.split()[0]) * 1024
    vm = {}
    for line in Path("/proc/vmstat").read_text().splitlines():
        name, value = line.split()
        if name in {"pswpin", "pswpout", "pgmajfault"}:
            vm[name] = int(value)
    pressure = {}
    path = Path("/proc/pressure/memory")
    if path.exists():
        for line in path.read_text().splitlines():
            kind, *fields = line.split()
            pressure[kind] = {k: float(v) for k, v in (field.split("=") for field in fields)}
    cpu = [int(value) for value in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
    usage = resource.getrusage(resource.RUSAGE_SELF)
    rss = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    return {
        "monotonic_s": time.monotonic(),
        "memory_bytes": memory,
        "vm": vm,
        "memory_pressure": pressure,
        "host_cpu_ticks": cpu,
        "client_cpu_s": usage.ru_utime + usage.ru_stime,
        "client_rss_bytes": rss,
    }


class HostMonitor:
    def __init__(self, path: Path):
        self.path = path

    def sample(self, phase: str) -> None:
        with self.path.open("a") as file:
            file.write(json.dumps({"phase": phase, **snapshot()}, separators=(",", ":")) + "\n")

    async def poll(self, interval_s: float = 1.0) -> None:
        while True:
            self.sample("measurement")
            await asyncio.sleep(interval_s)


def summarize_host(path: Path) -> dict:
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    first = next(row for row in rows if row["phase"] == "measurement_start")
    last = next(row for row in rows if row["phase"] == "measurement_end")
    elapsed = last["monotonic_s"] - first["monotonic_s"]

    def delta(key):
        return last["vm"][key] - first["vm"][key]

    full_first = first["memory_pressure"].get("full", {}).get("total")
    full_last = last["memory_pressure"].get("full", {}).get("total")
    total_ticks = sum(last["host_cpu_ticks"]) - sum(first["host_cpu_ticks"])
    idle_ticks = sum(last["host_cpu_ticks"][3:5]) - sum(first["host_cpu_ticks"][3:5])
    return {
        "host_cpu_busy_fraction": 1 - idle_ticks / total_ticks if total_ticks else None,
        "host_available_min_bytes": min(r["memory_bytes"]["MemAvailable"] for r in rows),
        "host_swap_in_pages": delta("pswpin"),
        "host_swap_out_pages": delta("pswpout"),
        "host_major_faults": delta("pgmajfault"),
        "host_memory_full_stall_s": (full_last - full_first) / 1e6
        if full_first is not None and full_last is not None
        else None,
        "client_cpu_cores": (last["client_cpu_s"] - first["client_cpu_s"]) / elapsed,
        "client_sampled_rss_peak_bytes": max(r["client_rss_bytes"] for r in rows),
    }

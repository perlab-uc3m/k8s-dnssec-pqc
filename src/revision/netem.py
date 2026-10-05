"""Added network delay or loss on the CoreDNS pod interface.

The impairment is a netem qdisc on eth0 inside each CoreDNS pod's network namespace. It acts on
every packet the pod sends: DNS responses, SYN-ACKs and the pod's own API requests. Each
client round trip to the pod therefore grows by the configured delay, and each reply is
dropped with the configured probability, while the metrics
port-forward, which enters the pod over loopback, is unaffected. A redeployed pod gets a
fresh namespace, so nothing has to be removed between runs.
"""

from __future__ import annotations

import json
import subprocess


def netem_args(delay_ms: float = 0, loss_pct: float = 0) -> list[str]:
    """tc arguments for a fixed delay and/or independent random loss on pod egress."""
    if delay_ms < 0 or loss_pct < 0 or loss_pct >= 100 or (delay_ms == 0 and loss_pct == 0):
        raise ValueError("netem needs a positive delay or a loss percentage below 100")
    args = ["tc", "qdisc", "replace", "dev", "eth0", "root", "netem"]
    if delay_ms:
        args += ["delay", f"{delay_ms:g}ms"]
    if loss_pct:
        args += ["loss", f"{loss_pct:g}%"]
    # A large limit keeps netem itself from dropping packets when many are held.
    return args + ["limit", "100000"]


def _docker(node: str, *args: str, timeout: float = 20) -> str:
    return subprocess.check_output(["docker", "exec", node, *args], text=True, timeout=timeout)


def pod_pid(node: str, pod: str) -> int:
    """PID of the pod's coredns container, read through the node's CRI."""
    ids = _docker(
        node,
        "crictl",
        "ps",
        "-q",
        "--state",
        "Running",
        "--label",
        f"io.kubernetes.pod.name={pod}",
        "--label",
        "io.kubernetes.container.name=coredns",
    ).split()
    if len(ids) != 1:
        raise RuntimeError(f"Expected one running coredns container in {pod}, found {ids}")
    info = json.loads(_docker(node, "crictl", "inspect", ids[0]))
    pid = info.get("info", {}).get("pid")
    if not isinstance(pid, int) or pid <= 0:
        raise RuntimeError(f"No PID reported for {pod}")
    return pid


def apply_netem(pods: list[dict], delay_ms: float = 0, loss_pct: float = 0) -> list[dict]:
    """Install the qdisc on every pod and return what tc reports afterwards."""
    records = []
    for pod in pods:
        node = pod["node"]
        pid = pod_pid(node, pod["name"])
        enter = ["nsenter", "-t", str(pid), "-n"]
        _docker(node, *enter, *netem_args(delay_ms, loss_pct))
        shown = _docker(node, *enter, "tc", "qdisc", "show", "dev", "eth0")
        if (
            "netem" not in shown
            or (delay_ms and "delay" not in shown)
            or (loss_pct and "loss" not in shown)
        ):
            raise RuntimeError(f"netem not active on {pod['name']}: {shown.strip()}")
        records.append({"pod": pod["name"], "node": node, "pid": pid, "qdisc": shown.strip()})
    return records


def preflight(node: str) -> str:
    """Fail before a campaign starts if the host kernel lacks sch_netem."""
    # A throwaway network namespace needs no cleanup and no extra kernel modules.
    script = (
        "unshare -n sh -c 'ip link set lo up && "
        "tc qdisc add dev lo root netem delay 1ms && tc qdisc show dev lo'"
    )
    result = subprocess.run(
        ["docker", "exec", node, "sh", "-c", script], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"netem is unavailable in the Kind node: {result.stderr.strip()}. If the qdisc kind "
            "is unknown, run 'sudo modprobe sch_netem' on the host (Ubuntu: install "
            "linux-modules-extra-$(uname -r)) and start the command again."
        )
    shown = result.stdout
    if "netem" not in shown:
        raise RuntimeError(f"netem preflight did not install a qdisc: {shown}")
    return shown.strip()

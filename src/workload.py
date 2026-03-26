"""Kubernetes service churn generator for DNS record turnover."""

from __future__ import annotations

import logging
import random
import string
import threading
import time
from dataclasses import dataclass, field
from kubernetes import client, config

log = logging.getLogger(__name__)


class LiveServicePool:
    """Thread-safe pool of active service names.

    Shared between the churn generator (writer) and the query generator
    (reader) so that queries always target currently-live services.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._names: list[str] = []

    def add(self, name: str):
        with self._lock:
            self._names.append(name)

    def remove(self, name: str):
        with self._lock:
            try:
                self._names.remove(name)
            except ValueError:
                pass

    def random_name(self) -> str | None:
        """Return a random name from the pool, or None if empty."""
        with self._lock:
            return random.choice(self._names) if self._names else None

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._names)


@dataclass
class ChurnState:
    active_services: dict = field(default_factory=dict)
    created_total: int = 0
    deleted_total: int = 0


def random_name(prefix="svc", length=8):
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=length))
    return f"{prefix}-{suffix}"


def create_service(api: client.CoreV1Api, namespace: str) -> str:
    name = random_name()
    svc = client.V1Service(
        metadata=client.V1ObjectMeta(name=name, labels={"bench": "churn"}),
        spec=client.V1ServiceSpec(
            selector={"app": name},
            ports=[client.V1ServicePort(port=80, target_port=80)],
            type="ClusterIP",
        ),
    )
    api.create_namespaced_service(namespace=namespace, body=svc)
    return name


def delete_service(api: client.CoreV1Api, namespace: str, name: str):
    api.delete_namespaced_service(name=name, namespace=namespace)


def run_churn(
    namespace: str,
    churn_rate: float,
    duration: float,
    baseline_count: int = 20,
    kubeconfig: str | None = None,
    pool: LiveServicePool | None = None,
) -> ChurnState:
    """Generate service churn at a specified rate.

    Each tick creates one service and deletes one (if any exist beyond baseline),
    at average rate `churn_rate` events/s. Uses Poisson inter-arrival times.
    """
    if kubeconfig:
        config.load_kube_config(config_file=kubeconfig)
    else:
        config.load_kube_config()

    api = client.CoreV1Api()
    state = ChurnState()

    # Create baseline services
    log.info("Creating %d baseline services in namespace %s", baseline_count, namespace)
    for _ in range(baseline_count):
        name = create_service(api, namespace)
        state.active_services[name] = time.monotonic()
        state.created_total += 1
        if pool is not None:
            pool.add(name)

    log.info("Starting churn at %.1f svc/s for %.0fs", churn_rate, duration)
    start = time.monotonic()
    interval = 1.0 / churn_rate if churn_rate > 0 else float("inf")

    while time.monotonic() - start < duration:
        # Poisson inter-arrival
        sleep = random.expovariate(churn_rate) if churn_rate > 0 else duration
        time.sleep(min(sleep, duration - (time.monotonic() - start)))

        if time.monotonic() - start >= duration:
            break

        # Create a new service
        name = create_service(api, namespace)
        state.active_services[name] = time.monotonic()
        state.created_total += 1
        if pool is not None:
            pool.add(name)

        # Delete a random non-baseline service if pool is large enough
        if len(state.active_services) > baseline_count:
            victim = random.choice(list(state.active_services.keys()))
            try:
                delete_service(api, namespace, victim)
                del state.active_services[victim]
                state.deleted_total += 1
                if pool is not None:
                    pool.remove(victim)
            except client.exceptions.ApiException:
                pass

    log.info(
        "Churn complete: created=%d deleted=%d active=%d",
        state.created_total,
        state.deleted_total,
        len(state.active_services),
    )
    return state


def cleanup(namespace: str, kubeconfig: str | None = None):
    """Delete all bench services."""
    if kubeconfig:
        config.load_kube_config(config_file=kubeconfig)
    else:
        config.load_kube_config()

    api = client.CoreV1Api()
    svcs = api.list_namespaced_service(namespace=namespace, label_selector="bench=churn")
    deleted = 0
    for svc in svcs.items:
        try:
            api.delete_namespaced_service(name=svc.metadata.name, namespace=namespace)
            deleted += 1
        except client.exceptions.ApiException:
            pass
    log.info("Cleaned up %d/%d bench services in %s", deleted, len(svcs.items), namespace)

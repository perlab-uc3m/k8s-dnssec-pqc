"""Fixed-name headless Services with deterministic EndpointSlice changes."""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException


def endpoint_address(index: int, version: int) -> str:
    if version < 0 or version >= 62500:
        raise ValueError("version space exhausted for one service")
    return f"10.{250 - version // 250}.{index % 250}.{version % 250 + 1}"


class HeadlessWorkload:
    def __init__(
        self,
        namespace: str,
        domain: str,
        count: int,
        kube_context: str,
        seed: int,
    ):
        if count < 1 or count > 250:
            raise ValueError("headless names must be between 1 and 250")
        config.load_kube_config(context=kube_context)
        self.core = client.CoreV1Api()
        self.discovery = client.DiscoveryV1Api()
        self.namespace, self.domain, self.count = namespace, domain, count
        self.names = [f"pqc-{i:03d}" for i in range(count)]
        self.versions = [0] * count
        self.rng = random.Random(seed)

    def fqdn(self, index: int) -> str:
        return f"{self.names[index]}.{self.namespace}.svc.{self.domain}."

    def setup(self) -> None:
        for index, name in enumerate(self.names):
            try:
                self.core.read_namespaced_service(name, self.namespace)
            except ApiException as exc:
                if exc.status != 404:
                    raise
                self.core.create_namespaced_service(
                    self.namespace,
                    client.V1Service(
                        metadata=client.V1ObjectMeta(
                            name=name,
                            labels={"bench": "pqc-revision"},
                        ),
                        spec=client.V1ServiceSpec(
                            cluster_ip="None",
                            ports=[
                                client.V1ServicePort(
                                    name="http",
                                    port=80,
                                    target_port=80,
                                )
                            ],
                        ),
                    ),
                )
            endpoint = {
                "addresses": [endpoint_address(index, 0)],
                "conditions": {"ready": True},
            }
            slice_name = f"{name}-managed"
            labels = {
                "kubernetes.io/service-name": name,
                "endpointslice.kubernetes.io/managed-by": "pqc-revision",
                "bench": "pqc-revision",
            }
            try:
                self.discovery.read_namespaced_endpoint_slice(
                    slice_name,
                    self.namespace,
                )
                self.discovery.patch_namespaced_endpoint_slice(
                    slice_name,
                    self.namespace,
                    {"endpoints": [endpoint]},
                )
            except ApiException as exc:
                if exc.status != 404:
                    raise
                self.discovery.create_namespaced_endpoint_slice(
                    self.namespace,
                    client.V1EndpointSlice(
                        address_type="IPv4",
                        metadata=client.V1ObjectMeta(
                            name=slice_name,
                            labels=labels,
                        ),
                        endpoints=[
                            client.V1Endpoint(
                                addresses=[endpoint_address(index, 0)],
                                conditions=client.V1EndpointConditions(ready=True),
                            )
                        ],
                        ports=[
                            client.DiscoveryV1EndpointPort(
                                name="http",
                                port=80,
                                protocol="TCP",
                            )
                        ],
                    ),
                )
            self.versions[index] = 0

    def _update(self, index: int, version: int) -> str:
        name = self.names[index]
        result = self.discovery.patch_namespaced_endpoint_slice(
            f"{name}-managed",
            self.namespace,
            {
                "endpoints": [
                    {
                        "addresses": [endpoint_address(index, version)],
                        "conditions": {"ready": True},
                    }
                ]
            },
            _request_timeout=(2, 5),
        )
        self.versions[index] = version
        return result.metadata.resource_version

    async def changes(
        self,
        output: Path,
        anchor: float,
        duration_s: float,
        rate: float,
        seed: int | None = None,
        batch: int = 1,
    ) -> dict:
        """Start when called. Every acknowledged update has a versioned address.

        batch=1 gives Poisson updates. batch>1 models a rollout: every batch/rate seconds,
        after a seeded random phase, batch distinct names change back to back. Both keep
        the same mean update rate.
        """
        if rate < 0:
            raise ValueError("negative update rate")
        if batch < 1 or batch > self.count:
            raise ValueError("update batch must be between 1 and the number of names")
        planned = 0.0
        count, errors, missed, offered = 0, 0, 0, 0
        rng = self.rng if seed is None else random.Random(seed)
        schedule: list[tuple[float, int]] = []
        if rate and batch > 1:
            interval = batch / rate
            start = rng.uniform(0, interval)
            while start < duration_s:
                schedule.extend((start, index) for index in rng.sample(range(self.count), batch))
                start += interval
        pending = iter(schedule)
        with (output / "updates.jsonl").open("w") as file:
            while rate:
                if batch > 1:
                    item = next(pending, None)
                    if item is None:
                        break
                    planned, index = item
                else:
                    planned += rng.expovariate(rate)
                    if planned >= duration_s:
                        break
                await asyncio.sleep(max(0.0, anchor + planned - time.monotonic()))
                if batch == 1:
                    index = rng.randrange(self.count)
                version = self.versions[index] + 1
                requested = time.monotonic() - anchor
                offered += 1
                dispatched = requested < duration_s
                if not dispatched:
                    missed += 1
                    file.write(
                        json.dumps(
                            {
                                "name": self.fqdn(index),
                                "planned_s": planned,
                                "requested_s": requested,
                                "dispatched": False,
                                "error": "update_deadline_missed",
                            }
                        )
                        + "\n"
                    )
                    continue
                self.versions[index] = version
                try:
                    resource_version = await asyncio.to_thread(
                        self._update,
                        index,
                        version,
                    )
                    error = None
                    count += 1
                except Exception as exc:
                    resource_version = None
                    error = f"{type(exc).__name__}: {exc}"
                    errors += 1
                doc = {
                    "name": self.fqdn(index),
                    "index": index,
                    "version": version,
                    "expected_a": endpoint_address(index, version),
                    "planned_s": planned,
                    "dispatched": True,
                    "requested_s": requested,
                    "acknowledged_s": time.monotonic() - anchor,
                    "resource_version": resource_version,
                    "error": error,
                }
                file.write(json.dumps(doc) + "\n")
                file.flush()
        return {
            "acknowledged": count,
            "errors": errors,
            "offered": offered,
            "deadline_missed": missed,
            "rate_requested": rate,
            "batch": batch,
        }

    def cleanup(self) -> None:
        selector = "bench=pqc-revision"
        for item in self.discovery.list_namespaced_endpoint_slice(
            self.namespace,
            label_selector=selector,
        ).items:
            self.discovery.delete_namespaced_endpoint_slice(
                item.metadata.name,
                self.namespace,
            )
        for item in self.core.list_namespaced_service(
            self.namespace,
            label_selector=selector,
        ).items:
            self.core.delete_namespaced_service(
                item.metadata.name,
                self.namespace,
            )

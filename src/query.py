"""Concurrent DNS query generator with Poisson inter-arrival scheduling."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import dns.message
import dns.query
import dns.rdatatype

log = logging.getLogger(__name__)


@dataclass
class QueryResult:
    timestamp: float
    service_name: str
    latency_ms: float
    rcode: int
    response_size: int
    had_rrsig: bool
    transport: str = "udp"  # "udp" or "tcp"
    error: str | None = None


def build_query(fqdn: str) -> dns.message.Message:
    q = dns.message.make_query(fqdn, dns.rdatatype.A, want_dnssec=True)
    q.flags |= dns.flags.AD
    return q


def send_query(
    server: str, port: int, fqdn: str, timeout: float = 5.0, tcp_port: int | None = None
) -> QueryResult:
    t0 = time.monotonic()
    try:
        q = build_query(fqdn)
        # Try UDP first, fall back to TCP on truncation
        resp = dns.query.udp(q, server, port=port, timeout=timeout)
        transport = "udp"
        if resp.flags & dns.flags.TC:
            tcp_p = tcp_port if tcp_port is not None else port
            resp = dns.query.tcp(q, server, port=tcp_p, timeout=timeout)
            transport = "tcp"

        latency = (time.monotonic() - t0) * 1000.0
        had_rrsig = any(rrset.rdtype == dns.rdatatype.RRSIG for rrset in resp.answer)
        return QueryResult(
            timestamp=t0,
            service_name=fqdn,
            latency_ms=latency,
            rcode=resp.rcode(),
            response_size=len(resp.to_wire()),
            had_rrsig=had_rrsig,
            transport=transport,
        )
    except Exception as e:
        latency = (time.monotonic() - t0) * 1000.0
        return QueryResult(
            timestamp=t0,
            service_name=fqdn,
            latency_ms=latency,
            rcode=-1,
            response_size=0,
            had_rrsig=False,
            error=str(e),
        )


def run_queries(
    server: str,
    port: int,
    service_names: list[str],
    namespace: str,
    domain: str,
    query_rate: float,
    duration: float,
    tcp_port: int | None = None,
    name_provider: Callable[[], str | None] | None = None,
) -> list[QueryResult]:
    """Send DNS queries at the given rate for the given duration."""
    futures = []
    # Size pool so that worst-case in-flight queries (all timing out) fit
    max_workers = max(32, min(int(query_rate * 6), 512))
    start = time.monotonic()

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        while True:
            elapsed = time.monotonic() - start
            if elapsed >= duration:
                break
            name = name_provider() if name_provider is not None else None
            if name is None:
                name = random.choice(service_names)
            fqdn = f"{name}.{namespace}.svc.{domain}"
            futures.append(pool.submit(send_query, server, port, fqdn, 5.0, tcp_port))

            # Poisson inter-arrival
            if query_rate > 0:
                sleep = random.expovariate(query_rate)
                remaining = duration - (time.monotonic() - start)
                if remaining > 0:
                    time.sleep(min(sleep, remaining))

        # Wait for all in-flight queries (timeout + margin)
        results = []
        for fut in futures:
            try:
                results.append(fut.result(timeout=10))
            except Exception:
                pass  # send_query already catches all exceptions

    log.info(
        "Queries complete: %d total, %d errors",
        len(results),
        sum(1 for r in results if r.error is not None),
    )
    return results

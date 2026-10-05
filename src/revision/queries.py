"""Bounded, open-loop DNS client with original wire capture.

All times in records are seconds from the run's monotonic anchor. The writer
drains a bounded queue and never changes response timings.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import random
import socket
import struct
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

import dns.flags
import dns.message
import dns.rcode
import dns.rdatatype


@dataclass
class QueryRecord:
    query_id: int
    fqdn: str
    planned_s: float
    dns_id: int | None = None
    admitted_s: float | None = None
    sent_s: float | None = None
    completed_s: float | None = None
    status: str = "client_rejected"
    rcode: int | None = None
    answer_a: list[str] = field(default_factory=list)
    answer_ttls: list[int] = field(default_factory=list)
    rrsig_original_ttls: list[int] = field(default_factory=list)
    rrsig_answer: int = 0
    rrsig_authority: int = 0
    rrsig_additional: int = 0
    # Write attempts are retained even if no response arrives.
    transmissions: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    error: str | None = None
    latency_ms: float | None = None
    wire_latency_ms: float | None = None
    dispatch_lag_ms: float | None = None
    verification: str = "not_checked"
    raw_responses: list[bytes] = field(default_factory=list, repr=False)


class ReusedTCP:
    """One in-flight request per connection; pool wait remains visible."""

    def __init__(self, host: str, port: int, size: int):
        self.host, self.port = host, port
        self.slots: asyncio.Queue[tuple | None] = asyncio.Queue(maxsize=size)
        for _ in range(size):
            self.slots.put_nowait(None)

    async def query(
        self, wire: bytes, deadline: float, on_send: Callable[[float], None] | None = None
    ) -> tuple[bytes, float]:
        slot = await self.slots.get()
        try:
            if slot is None or slot[1].is_closing():
                slot = await asyncio.open_connection(self.host, self.port)
            reader, writer = slot
            sent = time.monotonic()
            if on_send is not None:
                on_send(sent)
            writer.write(struct.pack(">H", len(wire)) + wire)
            await writer.drain()
            length = struct.unpack(">H", await reader.readexactly(2))[0]
            response = await reader.readexactly(length)
            return response, sent
        except BaseException:
            if slot is not None:
                slot[1].close()
            slot = None
            raise
        finally:
            self.slots.put_nowait(slot)

    async def close(self) -> None:
        while not self.slots.empty():
            slot = self.slots.get_nowait()
            if slot is not None:
                slot[1].close()
                try:
                    await slot[1].wait_closed()
                except OSError:
                    pass


async def _udp(
    host: str,
    port: int,
    wire: bytes,
    on_send: Callable[[float], None] | None = None,
    retry_s: float | None = None,
) -> tuple[bytes, float]:
    """One UDP exchange. With retry_s, resend the same query after each silent interval.

    The outer deadline still bounds the exchange. Any reply on the socket ends it, so a
    late answer to an earlier copy counts like a stub resolver would count it.
    """
    loop = asyncio.get_running_loop()
    addr = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
    sock = socket.socket(addr[0], socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        await loop.sock_connect(sock, addr[4])
        sent = time.monotonic()
        if on_send is not None:
            on_send(sent)
        await loop.sock_sendall(sock, wire)
        if retry_s is None:
            return await loop.sock_recv(sock, 65535), sent
        while True:
            try:
                return await asyncio.wait_for(loop.sock_recv(sock, 65535), retry_s), sent
            except TimeoutError:
                if on_send is not None:
                    on_send(time.monotonic())
                await loop.sock_sendall(sock, wire)
    finally:
        sock.close()


async def _tcp(
    host: str, port: int, wire: bytes, on_send: Callable[[float], None] | None = None
) -> tuple[bytes, float]:
    reader, writer = await asyncio.open_connection(host, port)
    try:
        sent = time.monotonic()
        if on_send is not None:
            on_send(sent)
        writer.write(struct.pack(">H", len(wire)) + wire)
        await writer.drain()
        length = struct.unpack(">H", await reader.readexactly(2))[0]
        return await reader.readexactly(length), sent
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def _classify(record: QueryRecord, wire: bytes, fqdn: str) -> bool:
    response = dns.message.from_wire(wire, raise_on_truncation=False)
    question_matches = (
        len(response.question) == 1
        and response.question[0].name.to_text().lower() == fqdn.rstrip(".").lower() + "."
        and response.question[0].rdtype == dns.rdatatype.A
        and response.question[0].rdclass == 1
    )
    if (
        not response.flags & dns.flags.QR
        or response.opcode() != 0
        or not question_matches
        or response.id != record.dns_id
    ):
        raise ValueError("DNS response header or question does not match request")
    record.rcode = response.rcode()
    record.answer_ttls = [rrset.ttl for rrset in response.answer]
    record.rrsig_original_ttls = [
        rr.original_ttl
        for rrset in response.answer
        if rrset.rdtype == dns.rdatatype.RRSIG
        for rr in rrset
    ]
    record.rrsig_answer = sum(s.rdtype == dns.rdatatype.RRSIG for s in response.answer)
    record.rrsig_authority = sum(s.rdtype == dns.rdatatype.RRSIG for s in response.authority)
    record.rrsig_additional = sum(s.rdtype == dns.rdatatype.RRSIG for s in response.additional)
    wanted = fqdn.rstrip(".").lower() + "."
    record.answer_a = sorted(
        rr.to_text()
        for rrset in response.answer
        if rrset.rdtype == dns.rdatatype.A and rrset.name.to_text().lower() == wanted
        for rr in rrset
    )
    if response.flags & dns.flags.TC:
        record.status = "truncated"
        return True
    if record.rcode == dns.rcode.NOERROR and record.answer_a:
        record.status = "positive_unverified"
    elif record.rcode == dns.rcode.NXDOMAIN:
        record.status = "negative_unverified"
    elif record.rcode == dns.rcode.SERVFAIL:
        record.status = "servfail"
    elif record.rcode == dns.rcode.NOERROR:
        record.status = "empty_unverified"
    else:
        record.status = "dns_error"
    return False


async def _one(
    record: QueryRecord,
    anchor: float,
    host: str,
    udp_port: int,
    tcp_port: int,
    policy: str,
    timeout_s: float,
    pool: ReusedTCP | None,
    udp_retry_s: float | None = None,
) -> QueryRecord:
    record.admitted_s = time.monotonic() - anchor
    query = dns.message.make_query(
        record.fqdn,
        dns.rdatatype.A,
        want_dnssec=True,
        use_edns=0,
        payload=1232,
    )
    wire = query.to_wire()
    record.dns_id = query.id
    deadline = anchor + record.planned_s + timeout_s
    try:
        async with asyncio.timeout_at(deadline):
            if policy == "fresh_fallback":
                modes = ["udp", "tcp"]
            elif policy == "fresh_tcp":
                modes = ["tcp"]
            elif policy == "reused_tcp":
                modes = ["reused_tcp"]
            else:
                raise ValueError(f"Unknown transport policy: {policy}")
            for mode in modes:

                def on_send(sent: float, transport: str = mode) -> None:
                    # Timestamp the client write attempt, not confirmed wire delivery.
                    record.transmissions.append(
                        {"transport": transport, "sent_s": sent - anchor, "wire_bytes": len(wire)}
                    )
                    if record.sent_s is None:
                        record.sent_s = sent - anchor

                if mode == "udp":
                    response, sent = await _udp(host, udp_port, wire, on_send, udp_retry_s)
                elif mode == "tcp":
                    response, sent = await _tcp(host, tcp_port, wire, on_send)
                else:
                    assert pool is not None
                    response, sent = await pool.query(wire, deadline, on_send)
                record.raw_responses.append(response)
                attempt = {
                    "transport": mode,
                    "sent_s": sent - anchor,
                    "received_s": time.monotonic() - anchor,
                    "wire_bytes": len(response),
                    "truncated": None,
                }
                record.attempts.append(attempt)
                truncated = _classify(record, response, record.fqdn)
                attempt["truncated"] = truncated
                if not truncated:
                    break
            if record.status == "truncated":
                record.status = "truncated_final"
    except TimeoutError:
        record.status = "timeout"
        record.error = "end_to_end_deadline"
    except Exception as exc:
        record.status = "transport_error"
        record.error = f"{type(exc).__name__}: {exc}"
    finally:
        record.completed_s = time.monotonic() - anchor
        record.latency_ms = (record.completed_s - record.planned_s) * 1000
        if record.sent_s is not None:
            record.wire_latency_ms = (record.completed_s - record.sent_s) * 1000
            record.dispatch_lag_ms = (record.sent_s - record.planned_s) * 1000
    return record


async def run_load(
    output: Path | None,
    host: str,
    udp_port: int,
    tcp_port: int,
    names: list[str],
    rate: float,
    duration_s: float,
    seed: int,
    policy: str = "fresh_fallback",
    timeout_s: float = 5.0,
    max_inflight: int = 256,
    tcp_connections: int = 16,
    name_provider: Callable[[], str] | None = None,
    at_window_end: Callable[[], Awaitable[None]] | None = None,
    anchor: float | None = None,
    utc_anchor: float | None = None,
    arrival_mode: str = "poisson",
    popularity: str = "uniform",
    tcp_pool: ReusedTCP | None = None,
    udp_retry_s: float | None = None,
) -> dict:
    """Run a bounded measurement. output=None performs a real query warmup."""
    if rate <= 0 or duration_s <= 0 or max_inflight <= 0 or not names:
        raise ValueError("positive rate, duration, capacity, and names required")
    if timeout_s <= 0 or tcp_connections < 1:
        raise ValueError("positive timeout and TCP pool size required")
    if udp_retry_s is not None and udp_retry_s <= 0:
        raise ValueError("udp_retry_s must be positive")
    if policy not in {"fresh_fallback", "fresh_tcp", "reused_tcp"}:
        raise ValueError(f"Unknown policy: {policy}")
    if arrival_mode not in {"poisson", "burst_2_of_20"}:
        raise ValueError(f"Unknown arrival mode: {arrival_mode}")
    if popularity not in {"uniform", "zipf_1_1"}:
        raise ValueError(f"Unknown popularity: {popularity}")
    if output is not None:
        output.mkdir(parents=True, exist_ok=True)
    arrival_rng = random.Random(seed)
    name_rng = random.Random(seed ^ 0x7C9E34B1)
    weights = [1 / ((i + 1) ** 1.1) for i in range(len(names))]
    high_s = int(duration_s // 20.0) * 2.0 + min(2.0, duration_s % 20.0)
    burst_normalizer = 1.0 + 9.0 * high_s / duration_s
    anchor = time.monotonic() if anchor is None else anchor
    utc_anchor = time.time() if utc_anchor is None else utc_anchor
    queue: asyncio.Queue[QueryRecord | None] = asyncio.Queue(maxsize=max_inflight * 2)
    if tcp_pool is not None and policy != "reused_tcp":
        raise ValueError("A shared TCP pool requires reused_tcp policy")
    pool = tcp_pool or (
        ReusedTCP(host, tcp_port, tcp_connections) if policy == "reused_tcp" else None
    )
    counts: Counter[str] = Counter()
    files = None
    if output is not None:
        files = (
            gzip.open(output / "queries.jsonl.gz", "wt"),
            gzip.open(output / "responses.bin.gz", "wb"),
        )

    async def writer() -> None:
        while True:
            record = await queue.get()
            if record is None:
                return
            counts[record.status] += 1
            if files is None:
                continue
            for attempt, raw in enumerate(record.raw_responses):
                files[1].write(struct.pack(">QBI", record.query_id, attempt, len(raw)))
                files[1].write(raw)
            doc = asdict(record)
            del doc["raw_responses"]
            files[0].write(json.dumps(doc, separators=(",", ":")) + "\n")

    running: set[asyncio.Task] = set()

    async def submit(record: QueryRecord) -> None:
        try:
            await queue.put(
                await _one(
                    record,
                    anchor,
                    host,
                    udp_port,
                    tcp_port,
                    policy,
                    timeout_s,
                    pool,
                    udp_retry_s,
                )
            )
        finally:
            running.discard(asyncio.current_task())

    offered = 0
    elapsed = 0.0
    try:
        async with asyncio.TaskGroup() as group:
            writer_task = group.create_task(writer())
            while True:
                if arrival_mode == "poisson":
                    elapsed += arrival_rng.expovariate(rate)
                else:
                    phase = elapsed % 20.0
                    segment_end = elapsed - phase + (2.0 if phase < 2.0 else 20.0)
                    segment_rate = rate * (10.0 if phase < 2.0 else 1.0) / burst_normalizer
                    candidate = elapsed + arrival_rng.expovariate(segment_rate)
                    elapsed = min(candidate, segment_end)
                    if candidate >= segment_end:
                        continue
                if elapsed >= duration_s:
                    break
                await asyncio.sleep(max(0.0, anchor + elapsed - time.monotonic()))
                if name_provider is not None:
                    fqdn = name_provider()
                elif popularity == "uniform":
                    fqdn = name_rng.choice(names)
                else:
                    fqdn = name_rng.choices(names, weights=weights, k=1)[0]
                record = QueryRecord(query_id=offered, fqdn=fqdn, planned_s=elapsed)
                offered += 1
                if len(running) >= max_inflight:
                    record.completed_s = time.monotonic() - anchor
                    record.error = "max_inflight"
                    record.latency_ms = (record.completed_s - elapsed) * 1000
                    await queue.put(record)
                    continue
                task = group.create_task(submit(record))
                running.add(task)
            await asyncio.sleep(max(0.0, anchor + duration_s - time.monotonic()))
            if at_window_end is not None:
                await at_window_end()
            if running:
                await asyncio.gather(*running)
            await queue.put(None)
            await writer_task
    finally:
        if pool is not None and tcp_pool is None:
            await pool.close()
        for task in running:
            task.cancel()
        if files is not None:
            for f in files:
                f.close()
    return {
        "schema_version": 1,
        "utc_anchor": utc_anchor,
        "monotonic_anchor": anchor,
        "duration_s": duration_s,
        "drain_s": time.monotonic() - anchor - duration_s,
        "offered": offered,
        "counts": dict(counts),
        "seed": seed,
        "transport_policy": policy,
        "rate_requested": rate,
        "arrival_mode": arrival_mode,
        "popularity": popularity,
        "max_inflight": max_inflight,
        "tcp_connections": tcp_connections if pool else 0,
        "udp_retry_s": udp_retry_s,
        "verification": "not_checked",
    }

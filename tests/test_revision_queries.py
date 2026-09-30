import asyncio
import gzip
import json
import struct

import dns.message
import dns.rrset

from src.revision.queries import run_load


class UDPServer(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        q = dns.message.from_wire(data)
        response = dns.message.make_response(q)
        response.answer.append(
            dns.rrset.from_text(
                q.question[0].name.to_text(),
                5,
                "IN",
                "A",
                "10.240.1.1",
            )
        )
        self.transport.sendto(response.to_wire(), addr)


async def _tcp_reply(reader, writer, delay=0):
    try:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
        q = dns.message.from_wire(await reader.readexactly(length))
        await asyncio.sleep(delay)
        response = dns.message.make_response(q)
        response.answer.append(
            dns.rrset.from_text(
                q.question[0].name.to_text(),
                5,
                "IN",
                "A",
                "10.240.1.1",
            )
        )
        wire = response.to_wire()
        writer.write(struct.pack(">H", len(wire)) + wire)
        await writer.drain()
    except asyncio.IncompleteReadError, ConnectionResetError:
        pass
    finally:
        writer.close()


def test_capture_and_latency(tmp_path):
    async def scenario():
        loop = asyncio.get_running_loop()
        udp, _ = await loop.create_datagram_endpoint(
            UDPServer,
            local_addr=("127.0.0.1", 0),
        )
        tcp = await asyncio.start_server(_tcp_reply, "127.0.0.1", 0)
        try:
            result = await run_load(
                tmp_path,
                "127.0.0.1",
                udp.get_extra_info("sockname")[1],
                tcp.sockets[0].getsockname()[1],
                ["a.bench.svc.cluster.local."],
                40,
                0.5,
                7,
                max_inflight=16,
            )
            assert result["offered"] > 0
            rows = [
                json.loads(line)
                for line in gzip.open(
                    tmp_path / "queries.jsonl.gz",
                    "rt",
                )
            ]
            assert len(rows) == result["offered"]
            assert all(row["status"] == "positive_unverified" for row in rows)
            assert all(row["latency_ms"] >= row["wire_latency_ms"] for row in rows)
            frames = gzip.open(tmp_path / "responses.bin.gz", "rb").read()
            pos, count = 0, 0
            while pos < len(frames):
                _, _, size = struct.unpack(">QBI", frames[pos : pos + 13])
                pos += 13 + size
                count += 1
            assert pos == len(frames)
            assert count == len(rows)
        finally:
            udp.close()
            tcp.close()
            await tcp.wait_closed()

    asyncio.run(scenario())


def test_overload_is_recorded(tmp_path):
    async def scenario():
        async def slow(reader, writer):
            await _tcp_reply(reader, writer, 0.2)

        server = await asyncio.start_server(slow, "127.0.0.1", 0)
        try:
            result = await run_load(
                tmp_path,
                "127.0.0.1",
                0,
                server.sockets[0].getsockname()[1],
                ["a.bench.svc.cluster.local."],
                120,
                0.3,
                9,
                policy="fresh_tcp",
                max_inflight=2,
                timeout_s=1,
            )
            assert result["counts"].get("client_rejected", 0) > 0
            assert sum(result["counts"].values()) == result["offered"]
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_wrong_dns_id_is_rejected():
    import pytest

    from src.revision.queries import QueryRecord, _classify

    q = dns.message.make_query("a.bench.svc.cluster.local.", "A")
    response = dns.message.make_response(q)
    response.id = (q.id + 1) % 65536
    record = QueryRecord(query_id=0, fqdn=q.question[0].name.to_text(), planned_s=0)
    record.dns_id = q.id
    with pytest.raises(ValueError, match="does not match"):
        _classify(record, response.to_wire(), record.fqdn)


def test_malformed_response_keeps_received_bytes(tmp_path):
    class MalformedUDP(UDPServer):
        def datagram_received(self, data, addr):
            self.transport.sendto(b"bad", addr)

    async def scenario():
        loop = asyncio.get_running_loop()
        udp, _ = await loop.create_datagram_endpoint(MalformedUDP, local_addr=("127.0.0.1", 0))
        try:
            result = await run_load(
                tmp_path,
                "127.0.0.1",
                udp.get_extra_info("sockname")[1],
                0,
                ["a.bench.svc.cluster.local."],
                40,
                0.1,
                7,
            )
            rows = [json.loads(line) for line in gzip.open(tmp_path / "queries.jsonl.gz", "rt")]
            assert result["offered"] > 0
            assert all(
                row["status"] == "transport_error" and len(row["attempts"]) == 1 for row in rows
            )
            frames = gzip.open(tmp_path / "responses.bin.gz", "rb").read()
            assert len(frames) == len(rows) * 16
            assert frames[13:16] == b"bad"
        finally:
            udp.close()

    asyncio.run(scenario())


def test_writer_failure_cancels_load(tmp_path, monkeypatch):
    import pytest

    import src.revision.queries as queries

    real_dumps = queries.json.dumps

    def failing_dumps(value, **kwargs):
        if isinstance(value, dict) and "query_id" in value:
            raise OSError("simulated output failure")
        return real_dumps(value, **kwargs)

    monkeypatch.setattr(queries.json, "dumps", failing_dumps)

    async def scenario():
        loop = asyncio.get_running_loop()
        udp, _ = await loop.create_datagram_endpoint(UDPServer, local_addr=("127.0.0.1", 0))
        try:
            with pytest.raises(ExceptionGroup, match="TaskGroup") as error:
                await asyncio.wait_for(
                    run_load(
                        tmp_path,
                        "127.0.0.1",
                        udp.get_extra_info("sockname")[1],
                        0,
                        ["a.bench.svc.cluster.local."],
                        200,
                        10,
                        7,
                        max_inflight=2,
                    ),
                    timeout=1,
                )
            assert any(isinstance(exc, OSError) for exc in error.value.exceptions)
        finally:
            udp.close()

    asyncio.run(scenario())


def test_tcp_pool_survives_warmup(tmp_path):
    from src.revision.queries import ReusedTCP

    async def scenario():
        connections = 0

        async def reply(reader, writer):
            nonlocal connections
            connections += 1
            try:
                while True:
                    length = struct.unpack(">H", await reader.readexactly(2))[0]
                    query = dns.message.from_wire(await reader.readexactly(length))
                    response = dns.message.make_response(query)
                    response.answer.append(
                        dns.rrset.from_text(
                            query.question[0].name.to_text(), 5, "IN", "A", "10.1.1.1"
                        )
                    )
                    wire = response.to_wire()
                    writer.write(struct.pack(">H", len(wire)) + wire)
                    await writer.drain()
            except asyncio.IncompleteReadError:
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(reply, "127.0.0.1", 0)
        pool = ReusedTCP("127.0.0.1", server.sockets[0].getsockname()[1], 1)
        try:
            for output in (None, tmp_path):
                result = await run_load(
                    output,
                    "127.0.0.1",
                    0,
                    pool.port,
                    ["a.bench.svc.cluster.local."],
                    60,
                    0.15,
                    7,
                    policy="reused_tcp",
                    tcp_pool=pool,
                )
                assert result["offered"] > 0
                assert result["counts"] == {"positive_unverified": result["offered"]}
            assert connections == 1
            rows = [json.loads(line) for line in gzip.open(tmp_path / "queries.jsonl.gz", "rt")]
            assert all(row["answer_ttls"] == [5] for row in rows)
        finally:
            await pool.close()
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())


def test_timeout_retains_send_without_received_frame():
    import time

    from src.revision.queries import QueryRecord, ReusedTCP, _one

    async def scenario(policy):
        received = asyncio.Event()
        closed = asyncio.Event()

        async def silent(reader, writer):
            try:
                length = struct.unpack(">H", await reader.readexactly(2))[0]
                await reader.readexactly(length)
                received.set()
                await reader.read()  # Accept the query but never send a response.
            finally:
                writer.close()
                await writer.wait_closed()
                closed.set()

        server = await asyncio.start_server(silent, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        pool = ReusedTCP("127.0.0.1", port, 1)
        try:
            record = await _one(
                QueryRecord(1, "a.bench.svc.cluster.local.", 0),
                time.monotonic(),
                "127.0.0.1",
                0,
                port,
                policy,
                0.1,
                pool,
            )
            assert received.is_set()
            assert record.status == "timeout"
            assert record.sent_s is not None
            assert record.dispatch_lag_ms >= 0
            assert record.wire_latency_ms > 0
            assert len(record.transmissions) == 1
            assert record.transmissions[0]["transport"] in ("tcp", "reused_tcp")
            assert record.transmissions[0]["sent_s"] == record.sent_s
            assert record.attempts == []
            assert record.raw_responses == []
            await asyncio.wait_for(closed.wait(), 1)
        finally:
            await pool.close()
            server.close()
            await server.wait_closed()

    for policy in ("fresh_tcp", "reused_tcp"):
        asyncio.run(scenario(policy))

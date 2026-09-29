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

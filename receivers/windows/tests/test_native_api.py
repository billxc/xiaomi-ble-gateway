"""Linux loopback integration tests; no BLE hardware, HA or external network.

Run with python -m unittest discover -s tests -p test_native_api.py -v
from receivers/windows/. Uses the installed real aioesphomeapi Noise client.
All identities, advertisement bytes and PSKs below are synthetic test fixtures;
never use these keys in a real receiver configuration.
"""

import asyncio
import base64
from contextlib import suppress
from pathlib import Path
import struct
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aioesphomeapi import APIClient, api_pb2 as pb
from aioesphomeapi.core import InvalidEncryptionKeyAPIError, RequiresEncryptionAPIError
from aioesphomeapi.model import APIVersion
from noise.connection import NoiseConnection

import native_api
from native_api import NativeApiServer

PSK = base64.b64encode(bytes(range(32))).decode()
MAC = "A1:B2:C3:D4:E5:F6"
NAME = "windows-ble-test"
# Unknown MAC/unknown AD type, flags, FE95 service data, manufacturer data,
# zero-length terminator: the complete AD string must survive byte-for-byte.
AD = bytes.fromhex("02010604ee0123450a1695fe5020aa0102030405ff3412abcd00")
ADDRESS = 0xD1E2F3041526


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


class NativeApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stats = {"capture_packets": 77}
        self.server = NativeApiServer(
            NAME, MAC.lower(), PSK, friendly_name="Windows BLE Test",
            stats=self.stats, host="127.0.0.1", port=0,
        )
        await self.server.start()
        self.clients = []
        self.writers = []

    async def asyncTearDown(self):
        for client in self.clients:
            with suppress(Exception):
                await client.disconnect(force=True)
        for writer in self.writers:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
        await self.server.stop()
        self.assertEqual(self.stats["connections"], 0)
        self.assertEqual(self.stats["subscriptions"], 0)
        self.assertEqual(self.stats["handler_errors"], 0)

    async def client(self, *, psk=PSK, keepalive=20):
        client = APIClient(
            "127.0.0.1", self.server.port, noise_psk=psk,
            expected_name=NAME, expected_mac=MAC.replace(":", "").lower(),
            keepalive=keepalive,
        )
        self.clients.append(client)
        await client.connect(log_errors=False)
        return client

    async def ping(self, client):
        # Also a FIFO barrier after subscription/unsubscription messages.
        response = await client._connection.send_message_await_response(
            pb.PingRequest(), pb.PingResponse,
        )
        self.assertIsInstance(response, pb.PingResponse)

    async def subscribe(self, client):
        queue = asyncio.Queue()
        unsubscribe = client.subscribe_bluetooth_le_raw_advertisements(queue.put_nowait)
        await self.ping(client)
        return queue, unsubscribe

    async def next_ad(self, queue):
        response = await asyncio.wait_for(queue.get(), 2)
        self.assertEqual(len(response.advertisements), 1)
        return response.advertisements[0]

    async def test_ha_plaintext_probe_discovers_noise_required(self):
        with self.assertRaises(RequiresEncryptionAPIError):
            await self.client(psk=None)

    async def test_real_client_initialization_capabilities_and_control(self):
        client = await self.client()
        self.assertEqual(client.api_version, APIVersion(1, 19))
        self.assertEqual(client.cached_name, NAME)
        info, entities, services = await client.device_info_and_list_entities()
        self.assertEqual((entities, services), ([], []))
        # HA subscribes these even for a BLE-only node with no local entities.
        client.subscribe_home_assistant_states_and_services(
            on_state=lambda *_: None, on_service_call=lambda *_: None,
            on_state_sub=lambda *_: None, on_state_request=lambda *_: None,
        )
        await self.ping(client)
        self.assertEqual(info.name, NAME)
        self.assertEqual(info.friendly_name, "Windows BLE Test")
        self.assertEqual(info.mac_address, MAC)
        self.assertEqual(info.bluetooth_mac_address, MAC)
        self.assertEqual(info.bluetooth_proxy_feature_flags, 0x21)
        self.assertEqual(info.manufacturer, "Windows BLE Proxy")
        self.assertEqual(info.model, "WinRT passive receiver")
        self.assertEqual(info.project_name, "custom.windows_ble_proxy")
        self.assertEqual(info.project_version, "0.1.0")
        self.assertEqual(info.esphome_version, native_api.VERSION)
        self.assertFalse(info.uses_password)
        self.assertFalse(info.has_deep_sleep)
        self.assertTrue(info.api_encryption_supported)
        caps = await client.device_capabilities_compat(info)
        self.assertEqual(caps.bluetooth_proxy.feature_flags, 0x21)
        self.assertEqual(caps.bluetooth_proxy.mac_address, MAC)
        self.assertEqual(await client.list_entities_services(), ([], []))
        conn = client._connection
        auth = await conn.send_message_await_response(
            pb.AuthenticationRequest(), pb.AuthenticationResponse,
        )
        self.assertFalse(auth.invalid_password)
        client.subscribe_states(lambda _: self.fail("no entities expected"))
        conn.send_message(pb.SubscribeLogsRequest(level=5))
        conn.send_message(pb.GetTimeResponse(epoch_seconds=123))
        now = await conn.send_message_await_response(pb.GetTimeRequest(), pb.GetTimeResponse)
        self.assertLess(abs(now.epoch_seconds - int(time.time())), 3)
        await self.ping(client)
        self.assertEqual(self.stats["capture_packets"], 77)
        self.assertIs(self.server.stats, self.stats)
        await client.disconnect()
        await eventually(lambda: self.stats["connections"] == 0)

    async def test_raw_fanout_preserves_unknown_ad_and_fe95(self):
        self.server.publish(ADDRESS, -70, b"offline")
        first, second = await self.client(), await self.client()
        q1, _ = await self.subscribe(first)
        q2, _ = await self.subscribe(second)
        self.assertEqual(self.stats["subscriptions"], 2)
        self.server.publish(ADDRESS, -91, AD, 1)
        for queue in (q1, q2):
            ad = await self.next_ad(queue)
            self.assertEqual((ad.address, ad.rssi, ad.address_type, ad.data),
                             (ADDRESS, -91, 1, AD))
            self.assertTrue(queue.empty())
        await eventually(lambda: self.stats["packets_sent"] == 2)
        self.assertEqual(self.stats["advertisements_published"], 2)
        self.assertEqual(self.stats["drops"], 0)

    async def test_unsubscribe_and_reconnect_have_no_history(self):
        client = await self.client()
        queue, unsubscribe = await self.subscribe(client)
        self.server.publish(ADDRESS, -50, AD)
        self.assertEqual((await self.next_ad(queue)).data, AD)
        unsubscribe()
        await self.ping(client)
        self.assertEqual(self.stats["subscriptions"], 0)
        self.server.publish(ADDRESS, -51, b"unsubscribed")
        # On resubscription only the new packet may arrive.
        queue, _ = await self.subscribe(client)
        self.server.publish(ADDRESS + 1, -52, b"new")
        self.assertEqual((await self.next_ad(queue)).data, b"new")
        self.assertTrue(queue.empty())
        # Enqueue without yielding, then lose the session. None of this may
        # leak into another session's queue.
        for i in range(10):
            self.server.publish(ADDRESS, -60, bytes([i]))
        await client.disconnect(force=True)
        await eventually(lambda: self.stats["connections"] == 0)
        self.server.publish(ADDRESS, -53, b"disconnected")
        client = await self.client()
        queue, _ = await self.subscribe(client)
        self.server.publish(ADDRESS, -54, b"fresh")
        self.assertEqual((await self.next_ad(queue)).data, b"fresh")
        self.assertTrue(queue.empty())

    async def test_bounded_queue_drops_oldest_and_counts_per_subscriber(self):
        first, second = await self.client(), await self.client()
        q1, _ = await self.subscribe(first)
        q2, _ = await self.subscribe(second)
        capacity = native_api.ADVERTISEMENT_QUEUE_SIZE
        for i in range(capacity + 7):
            self.server.publish(ADDRESS, -80, i.to_bytes(2, "big"))
        self.assertEqual(self.stats["drops"], 14)
        self.assertTrue(all(s.queue.qsize() == capacity for s in self.server._sessions))
        for queue in (q1, q2):
            values = [int.from_bytes((await self.next_ad(queue)).data, "big")
                      for _ in range(capacity)]
            self.assertEqual(values, list(range(7, capacity + 7)))
        await eventually(lambda: self.stats["packets_sent"] == 2 * capacity)

    async def test_unsubscribe_discards_queued_deliveries(self):
        client = await self.client()
        queue, _ = await self.subscribe(client)
        session = next(iter(self.server._sessions))
        # Exercise a packet already dequeued but blocked behind a control
        # writer, plus the remaining queued packets.
        await session.write_lock.acquire()
        for _ in range(5):
            self.server.publish(ADDRESS, -80, AD)
        await eventually(lambda: session.queue.qsize() == 4)
        session._subscribe(False)
        session.write_lock.release()
        await self.ping(client)
        await eventually(lambda: self.stats["drops"] == 5)
        self.assertTrue(queue.empty())
        self.assertEqual(self.stats["packets_sent"], 0)

    async def test_resubscribe_does_not_revive_dequeued_old_advertisement(self):
        client = await self.client()
        queue, _ = await self.subscribe(client)
        session = next(iter(self.server._sessions))
        await session.write_lock.acquire()
        try:
            self.server.publish(ADDRESS, -80, b"old-generation")
            await eventually(lambda: session.queue.empty())
            session._subscribe(False)
            session._subscribe(True)
            self.server.publish(ADDRESS, -81, b"new-generation")
        finally:
            session.write_lock.release()
        self.assertEqual((await self.next_ad(queue)).data, b"new-generation")
        await self.ping(client)
        self.assertTrue(queue.empty())
        self.assertEqual(self.stats["drops"], 1)
        self.assertEqual(self.stats["packets_sent"], 1)

    async def test_disconnect_discards_blocked_and_queued_advertisements(self):
        client = await self.client()
        queue, _ = await self.subscribe(client)
        session = next(iter(self.server._sessions))
        await session.write_lock.acquire()
        try:
            for _ in range(5):
                self.server.publish(ADDRESS, -80, b"discard-on-close")
            await eventually(lambda: session.queue.qsize() == 4)
            # Abrupt TCP loss, not force_disconnect(), which still sends an
            # API DisconnectRequest and needs the deliberately held writer.
            session.writer.close()
            await eventually(lambda: self.stats["connections"] == 0)
        finally:
            session.write_lock.release()
        self.assertEqual(self.stats["drops"], 5)
        self.assertTrue(queue.empty())
        client = await self.client()
        queue, _ = await self.subscribe(client)
        self.server.publish(ADDRESS, -81, b"new-session")
        self.assertEqual((await self.next_ad(queue)).data, b"new-session")
        self.assertTrue(queue.empty())

    async def test_wrong_psk_rejected_and_plaintext_never_accepted(self):
        with self.assertRaises(InvalidEncryptionKeyAPIError):
            await self.client(psk=base64.b64encode(b"x" * 32).decode())
        await eventually(lambda: self.stats["connections"] == 0)
        self.assertEqual(self.stats["handshake_failures"], 1)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        self.writers.append(writer)
        writer.write(b"\x00\x00\x01")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2),
                         b"\x01\x00\x13\x01Bad indicator byte")
        await eventually(lambda: self.stats["handshake_failures"] == 2)
        client = await self.client()
        await self.ping(client)

    async def test_client_automatic_ping_keepalive(self):
        client = await self.client(keepalive=0.05)
        await eventually(lambda: self.stats["pings_received"] >= 3)
        self.assertEqual((await client.device_info()).name, NAME)
        self.assertEqual(self.stats["connections"], 1)

    async def test_connection_limit_handshake_timeout_and_stop(self):
        readers = []
        for _ in range(4):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
            self.writers.append(writer)
            readers.append(reader)
        await eventually(lambda: self.stats["connections"] == 4)
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        self.writers.append(writer)
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertEqual(self.stats["connections_rejected"], 1)
        await self.server.stop()
        self.assertTrue(all(task.done() for task in asyncio.all_tasks()
                            if task.get_name() == "native-api-session"))
        await self.server.stop()
        await self.server.start()
        with patch.object(native_api, "HANDSHAKE_TIMEOUT", 0.05):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
            self.writers.append(writer)
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        await eventually(lambda: self.stats["connections"] == 0)
        self.assertGreaterEqual(self.stats["handshake_failures"], 1)

    async def test_hello_extension_prologue_and_malformed_encrypted_length(self):
        # An independent initiator checks the real device's client-hello
        # extension/prologue rule and the encrypted inner length validation.
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        self.writers.append(writer)

        async def frame(payload):
            writer.write(b"\x01" + len(payload).to_bytes(2, "big") + payload)
            await writer.drain()

        async def read_frame():
            header = await reader.readexactly(3)
            self.assertEqual(header[0], 1)
            return await reader.readexactly(int.from_bytes(header[1:], "big"))

        extension = b"\x12\x34"
        noise = NoiseConnection.from_name(native_api.NOISE_PROTOCOL)
        noise.set_as_initiator()
        noise.set_psks(base64.b64decode(PSK))
        noise.set_prologue(b"NoiseAPIInit\x00\x02" + extension)
        noise.start_handshake()
        await frame(extension)
        self.assertEqual(await read_frame(), b"\x01" + NAME.encode() + b"\0"
                         + MAC.replace(":", "").lower().encode() + b"\0")
        await frame(b"\0" + bytes(noise.write_message()))
        handshake = await read_frame()
        self.assertEqual(handshake[0], 0)
        noise.read_message(handshake[1:])
        hello = pb.HelloRequest(api_version_major=1, api_version_minor=19).SerializeToString()
        await frame(noise.encrypt(struct.pack(">HH", 1, len(hello)) + hello))
        response = noise.decrypt(await read_frame())
        self.assertEqual(struct.unpack(">HH", response[:4]), (2, len(response) - 4))
        self.assertEqual(pb.HelloResponse.FromString(response[4:]).name, NAME)
        await frame(noise.encrypt(struct.pack(">HH", 7, 123)))
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        await eventually(lambda: self.stats["protocol_errors"] == 1)

    async def test_oversized_handshake_rejected_before_body_read(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        self.writers.append(writer)
        writer.write(b"\x01\x04\x01")  # 1025 bytes declared, body never supplied.
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        await eventually(lambda: self.stats["handshake_failures"] == 1)

    async def test_malformed_noise_handshake_is_protocol_rejection(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.server.port)
        self.writers.append(writer)
        writer.write(b"\x01\x00\x00\x01\x00\x02\x00x")
        await writer.drain()
        header = await reader.readexactly(3)
        await reader.readexactly(int.from_bytes(header[1:], "big"))
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        await eventually(lambda: self.stats["handshake_failures"] == 1)

    async def test_publish_enforces_encrypted_wire_frame_budget(self):
        with self.assertRaises(ValueError):
            self.server.publish(ADDRESS, -50, b"x" * 65536)
        # Overhead from address/data tags and the repeated-message wrapper
        # counts too, even if the AD bytes alone fit MAX_PROTOBUF_SIZE.
        with self.assertRaises(ValueError):
            self.server.publish(ADDRESS, -50, b"x" * native_api.MAX_PROTOBUF_SIZE)
        self.assertEqual(self.stats["advertisements_published"], 0)

    async def test_decoded_subscription_is_not_raw_and_logs_reason_once(self):
        client = await self.client()
        with self.assertLogs("native_api", level="INFO") as logs:
            client.subscribe_bluetooth_le_advertisements(lambda _: self.fail("decoded unsupported"))
            await self.ping(client)
            client.subscribe_bluetooth_le_advertisements(lambda _: None)
            await self.ping(client)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("RAW flag required", logs.output[0])
        self.assertEqual(self.stats["subscriptions"], 0)
        self.server.publish(ADDRESS, -50, AD)
        await self.ping(client)
        self.assertEqual(self.stats["packets_sent"], 0)


class ConfigurationTests(unittest.TestCase):
    def test_mandatory_valid_psk_and_stable_identity(self):
        for psk in (None, "", "not base64", base64.b64encode(b"short").decode()):
            with self.subTest(psk=psk), self.assertRaises(ValueError):
                NativeApiServer(NAME, MAC, psk)
        for mac in ("", "AABBCCDDEEFF", "GG:00:00:00:00:00"):
            with self.subTest(mac=mac), self.assertRaises(ValueError):
                NativeApiServer(NAME, mac, PSK)
        for name in ("", "name\0injection", "UPPER CASE"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                NativeApiServer(name, MAC, PSK)


if __name__ == "__main__":
    unittest.main()

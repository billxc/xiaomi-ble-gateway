"""Encrypted ESPHome Native API subset for a passive Windows BLE receiver.

All methods, including publish(), belong to the asyncio loop running start().
A capture thread must use loop.call_soon_threadsafe(server.publish, ...).
There is no plaintext transport, advertisement decoding, persistence or replay.

Wire contract: Noise_NNpsk0_25519_ChaChaPoly_SHA256, API 1.19, raw message 93
(one complete AD string per response). Frames are limited to 65535 bytes;
handshake frames to 1024 bytes. Only PASSIVE_SCAN | RAW_ADVERTISEMENTS (0x21)
are advertised. No GATT, OTA, password, remote execution or scanner switching.

stats is the caller's dict (other keys are preserved). Gauges: connections,
subscriptions. Cumulative counters: connections_total, connections_rejected,
handshake_failures, protocol_errors, handler_errors, advertisements_published,
messages_sent (encrypted API messages), packets_sent (raw advertisement frames),
drops (per-subscriber deliveries discarded on overflow/unsubscribe/disconnect),
pings_received. With no subscribers, publish increments published, not drops.
Sent means drained to the local TCP transport, not acknowledged by HA. Bytes
already handed to TCP cannot be retracted on unsubscribe. Queues are per-session,
128 entries, drop-oldest; disconnect discards them, never reconnects/replays.

Protocol sources: aioesphomeapi core.MESSAGE_TYPE_TO_PROTO/api_pb2 and ESPHome
api_connection.cpp, api_frame_helper_noise.cpp, api_pb2_service.cpp.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import re
import struct
import time
from contextlib import suppress

from protocol import api_pb2 as pb, api_options_pb2

# IDs live in the official schema; no device-side dependency on the client SDK.
MESSAGE_TYPE_TO_PROTO = {
    desc.GetOptions().Extensions[api_options_pb2.id]: getattr(pb, name)
    for name, desc in pb.DESCRIPTOR.message_types_by_name.items()
    if desc.GetOptions().HasExtension(api_options_pb2.id)
}
from cryptography.exceptions import InvalidTag
from google.protobuf.message import DecodeError, Message
from noise.connection import NoiseConnection
from noise.exceptions import NoiseInvalidMessage, NoiseValueError

_LOGGER = logging.getLogger(__name__)
NOISE_PROTOCOL = b"Noise_NNpsk0_25519_ChaChaPoly_SHA256"
VERSION = "2026.10.0-compat.0.1.0"
FEATURE_FLAGS = 0x21
MAX_CONNECTIONS = 4
HANDSHAKE_TIMEOUT = 10.0
WRITE_TIMEOUT = 10.0
MAX_FRAME_SIZE = 65535
MAX_HANDSHAKE_SIZE = 1024
MAX_PROTOBUF_SIZE = MAX_FRAME_SIZE - 4 - 16
ADVERTISEMENT_QUEUE_SIZE = 128
_HEADER = struct.Struct(">HH")
_RESPONSE_IDS = {cls: number for number, cls in MESSAGE_TYPE_TO_PROTO.items()}
_REQUEST_IDS = {1, 3, 5, 6, 7, 8, 9, 11, 20, 28, 34, 36, 37, 38, 66, 87, 149}
_COUNTERS = (
    "connections_total", "connections_rejected", "handshake_failures",
    "protocol_errors", "handler_errors", "advertisements_published",
    "messages_sent", "packets_sent", "drops", "pings_received", "unsubscribed_advertisements",
)


class _ProtocolError(Exception):
    """Invalid wire input; messages never contain peer bytes or secrets."""


class NativeApiServer:
    def __init__(
        self, name, mac_address, noise_psk, *, friendly_name=None, stats=None,
        host="0.0.0.0", port=6053,
    ):
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
            raise ValueError("name must be an ESPHome node name (1-63 lowercase letters/digits/hyphens)")
        if not isinstance(mac_address, str) or not re.fullmatch(
            r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", mac_address
        ):
            raise ValueError("mac_address must be a colon-separated six-byte MAC")
        try:
            psk = base64.b64decode(noise_psk, validate=True)
        except (ValueError, TypeError, binascii.Error):
            raise ValueError("noise_psk must be base64 encoding of 32 bytes") from None
        if len(psk) != 32:
            raise ValueError("noise_psk must be base64 encoding of 32 bytes")
        self.name = name
        self.mac_address = mac_address.upper()
        self.friendly_name = name if friendly_name is None else friendly_name
        self._psk = psk
        self.host = host
        self.port = port
        self.stats = {} if stats is None else stats
        for key in _COUNTERS:
            self.stats.setdefault(key, 0)
        self.stats.update(connections=0, subscriptions=0)
        self._server = None
        self._sessions: set[_Session] = set()
        self._stopping = False

    async def start(self):
        """Listen; port=0 selects an ephemeral port, exposed as server.port."""
        if self._server is not None:
            return
        self._stopping = False
        self._server = await asyncio.start_server(
            self._accept, self.host, self.port, limit=MAX_FRAME_SIZE + 3,
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        """Close listener and all sessions; discard unsent advertisements."""
        self._stopping = True
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        sessions = tuple(self._sessions)
        for session in sessions:
            session.close()
            session.task.cancel()
        await asyncio.gather(*(s.task for s in sessions), return_exceptions=True)
        self._sessions.clear()
        self.stats.update(connections=0, subscriptions=0)

    def _accept(self, reader, writer):
        if self._stopping or len(self._sessions) >= MAX_CONNECTIONS:
            self.stats["connections_rejected"] += 1
            writer.close()
            return
        session = _Session(self, reader, writer)
        self._sessions.add(session)
        self.stats["connections_total"] += 1
        self.stats["connections"] = len(self._sessions)
        session.task = asyncio.create_task(session.run(), name="native-api-session")

    def publish(self, address: int, rssi: int, data: bytes, address_type: int = 0):
        """Fan out a complete, unmodified AD string to current RAW subscribers.

        Invalid protobuf scalar values or oversized data raise ValueError. No MAC,
        AD-type or service-UUID filters are applied. Must run on the server loop.
        """
        if len(data) > MAX_PROTOBUF_SIZE:
            raise ValueError("advertisement exceeds Native API frame limit")
        response = pb.BluetoothLERawAdvertisementsResponse()
        response.advertisements.add(
            address=address, rssi=rssi, data=data, address_type=address_type,
        )
        payload = response.SerializeToString()
        if len(payload) > MAX_PROTOBUF_SIZE:
            raise ValueError("advertisement exceeds Native API frame limit")
        self.stats["advertisements_published"] += 1
        targets = [s for s in self._sessions if s.subscribed and not s.closing]
        if not targets:
            self.stats["unsubscribed_advertisements"] += 1
        for session in targets:
            if session.queue.full():
                session.queue.get_nowait()
                self.stats["drops"] += 1
            session.queue.put_nowait(payload)

    def _device_info(self):
        return pb.DeviceInfoResponse(
            name=self.name, friendly_name=self.friendly_name,
            mac_address=self.mac_address, bluetooth_mac_address=self.mac_address,
            esphome_version=VERSION, manufacturer="Windows BLE Proxy",
            model="WinRT passive receiver", project_name="custom.windows_ble_proxy",
            project_version="0.1.0", uses_password=False, has_deep_sleep=False,
            legacy_bluetooth_proxy_version=5, bluetooth_proxy_feature_flags=FEATURE_FLAGS,
            api_encryption_supported=True,
        )


class _Session:
    def __init__(self, server, reader, writer):
        self.server = server
        self.reader = reader
        self.writer = writer
        self.noise = None
        self.hello = False
        self.subscribed = False
        self.subscription_generation = 0
        self.closing = False
        self.decoded_warning = False
        self.queue = asyncio.Queue(maxsize=ADVERTISEMENT_QUEUE_SIZE)
        self.write_lock = asyncio.Lock()
        self.task = None

    def _subscribe(self, enabled):
        if self.subscribed != enabled:
            self.subscribed = enabled
            self.server.stats["subscriptions"] += 1 if enabled else -1
        if not enabled:
            self.subscription_generation += 1
            while not self.queue.empty():
                self.queue.get_nowait()
                self.server.stats["drops"] += 1

    def close(self):
        self.closing = True
        self._subscribe(False)
        self.writer.close()

    async def run(self):
        workers = []
        try:
            # Absolute deadline covers Noise AND API Hello, not reset by Ping.
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                await self._handshake()
                while not self.hello:
                    if not await self._receive():
                        return
            workers = [asyncio.create_task(self._read_loop()),
                       asyncio.create_task(self._advertisement_loop())]
            done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            if not self.hello:
                self.server.stats["handshake_failures"] += 1
        except (_ProtocolError, DecodeError, NoiseInvalidMessage, InvalidTag):
            key = "protocol_errors" if self.hello else "handshake_failures"
            self.server.stats[key] += 1
            _LOGGER.debug("Native API session rejected (%s)", key)
        except Exception as exc:
            self.server.stats["handler_errors"] += 1
            # Never log the exception text/traceback: it can contain peer data.
            _LOGGER.warning("Native API handler failed (%s)", type(exc).__name__)
        finally:
            self.close()
            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            with suppress(ConnectionError, OSError, TimeoutError):
                async with asyncio.timeout(1):
                    await self.writer.wait_closed()
            self.server._sessions.discard(self)
            self.server.stats["connections"] = len(self.server._sessions)

    async def _read_frame(self, limit=MAX_FRAME_SIZE):
        header = await self.reader.readexactly(3)
        size = int.from_bytes(header[1:], "big")
        if header[0] != 1:
            if self.noise is None:
                # ESPHome sends an explicit Noise reject, so HA's initial
                # plaintext probe can discover that an encryption key is required.
                await self._write_frame(b"\x01Bad indicator byte")
            raise _ProtocolError("invalid frame header")
        if size > limit:
            raise _ProtocolError("invalid frame header")
        return await self.reader.readexactly(size)

    async def _write_frame(self, payload):
        if len(payload) > MAX_FRAME_SIZE:
            raise _ProtocolError("outgoing frame too large")
        self.writer.write(b"\x01" + len(payload).to_bytes(2, "big") + payload)
        async with asyncio.timeout(WRITE_TIMEOUT):
            await self.writer.drain()

    async def _handshake(self):
        client_hello = await self._read_frame(MAX_HANDSHAKE_SIZE)
        await self._write_frame(
            b"\x01" + self.server.name.encode() + b"\0"
            + self.server.mac_address.replace(":", "").lower().encode() + b"\0"
        )
        noise = NoiseConnection.from_name(NOISE_PROTOCOL)
        noise.set_as_responder()
        noise.set_psks(self.server._psk)
        # Device firmware includes the client hello length and contents.
        noise.set_prologue(b"NoiseAPIInit" + len(client_hello).to_bytes(2, "big") + client_hello)
        noise.start_handshake()
        frame = await self._read_frame(MAX_HANDSHAKE_SIZE)
        if not frame or frame[0] != 0:
            raise _ProtocolError("invalid handshake status")
        try:
            noise.read_message(frame[1:])
        except InvalidTag:
            await self._write_frame(b"\x01Handshake MAC failure")
            raise
        except (ValueError, NoiseInvalidMessage, NoiseValueError):
            raise _ProtocolError("invalid Noise handshake") from None
        await self._write_frame(b"\x00" + bytes(noise.write_message()))
        self.noise = noise

    async def _send_payload(self, number, payload):
        async with self.write_lock:
            ciphertext = self.noise.encrypt(_HEADER.pack(number, len(payload)) + payload)
            await self._write_frame(ciphertext)
            self.server.stats["messages_sent"] += 1

    async def _send(self, message: Message):
        await self._send_payload(_RESPONSE_IDS[type(message)], message.SerializeToString())

    async def _read_loop(self):
        while await self._receive():
            pass

    async def _advertisement_loop(self):
        while True:
            payload = await self.queue.get()
            generation = self.subscription_generation
            try:
                # Recheck after acquiring the lock: unsubscribe can run while
                # a control response owns the writer. Never send a dequeued stale AD.
                async with self.write_lock:
                    if (not self.subscribed or self.closing
                            or generation != self.subscription_generation):
                        self.server.stats["drops"] += 1
                        continue
                    ciphertext = self.noise.encrypt(_HEADER.pack(93, len(payload)) + payload)
                    await self._write_frame(ciphertext)
                    self.server.stats["messages_sent"] += 1
                    self.server.stats["packets_sent"] += 1
            except BaseException:
                self.server.stats["drops"] += 1
                raise

    async def _receive(self):
        plaintext = self.noise.decrypt(await self._read_frame())
        if len(plaintext) < 4:
            raise _ProtocolError("short encrypted message")
        number, length = _HEADER.unpack_from(plaintext)
        if length != len(plaintext) - 4:
            raise _ProtocolError("protobuf length mismatch")
        if number not in _REQUEST_IDS:
            raise _ProtocolError("unsupported message")
        request = MESSAGE_TYPE_TO_PROTO[number].FromString(plaintext[4:])
        if number == 1:
            if self.hello:
                raise _ProtocolError("duplicate hello")
            await self._send(pb.HelloResponse(
                api_version_major=1, api_version_minor=19,
                name=self.server.name, server_info=VERSION,
            ))
            self.hello = True  # PSK + Hello authenticates; no deprecated auth needed.
        elif number == 5:
            self._subscribe(False)
            await self._send(pb.DisconnectResponse())
            return False
        elif number == 7:
            self.server.stats["pings_received"] += 1
            await self._send(pb.PingResponse())
        elif not self.hello:
            raise _ProtocolError("hello required")
        elif number == 3:
            await self._send(pb.AuthenticationResponse(invalid_password=False))
        elif number == 9:
            await self._send(self.server._device_info())
        elif number == 11:
            await self._send(pb.ListEntitiesDoneResponse())
        elif number == 36:
            await self._send(pb.GetTimeResponse(epoch_seconds=int(time.time())))
        elif number == 66:
            raw = bool(request.flags & 1)
            self._subscribe(raw)
            if not raw and not self.decoded_warning:
                self.decoded_warning = True
                _LOGGER.info("Native API decoded BLE subscription ignored: RAW flag required")
        elif number == 87:
            self._subscribe(False)
        elif number == 149:
            response = pb.DeviceCapabilitiesResponse()
            response.bluetooth_proxy.feature_flags = FEATURE_FLAGS
            response.bluetooth_proxy.mac_address = self.server.mac_address
            await self._send(response)
        # 6/8: response acknowledgments; 20/28: empty states/logs; 34/38:
        # HA always subscribes services/state requests even on entity-less nodes.
        # 37: host owns the clock, so never set system time from a client.
        return True

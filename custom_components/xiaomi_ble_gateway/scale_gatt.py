"""Minimal read-only Standard Auth / SPEC v2 history client (no HA imports).

Protocol/crypto layout adapted from dentra/esphome-components commit
38488b299cc9ecac0cc9f7cda0898c7bcc373f77 (MIT, Dennis Trachuk;
see third_party/dentra-standard-auth/LICENSE). The fail-open confirmation in
that implementation is deliberately NOT retained. SPEC byte formats follow
research/scale-gatt-protocol.md and its response-codec evidence, not vendor code.

Only business request: Action9.1, once. Transport SUCCESS is NOT Action9.2.
No registration, configuration, clock, offline confirmation, or cloud operations.
The connected client's lifecycle/disconnect belongs to the caller. Notification
callbacks only enqueue; all state, crypto and awaited persistence run in the
calling coroutine. Do not log exceptions/locals containing measurement data.
"""
from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import math
import secrets
import struct
from typing import Awaitable, Callable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import constant_time, hashes, hmac
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_BASE = "-0000-1000-8000-00805f9b34fb"
AUTH_CONTROL = "00000010" + _BASE
AUTH_DATA = "00000019" + _BASE
SPEC_RX = "0000001a" + _BASE
SPEC_TX = "0000001b" + _BASE
ACK_READY = b"\x00\x00\x01\x01"
ACK_SUCCESS = b"\x00\x00\x01\x00"
OPERATION_TIMEOUT = 10.0
QUIET_TIMEOUT = 3.0
# Local host inactivity policy, NOT a conversion of vendor timer units.
RECEIVE_IDLE_TIMEOUT = 0.4
MAX_REPAIR_ATTEMPTS = 3
QUEUE_SIZE = 512
MAX_PACKET = 8192


class _Failure(Exception):
    """Only fixed, non-sensitive status codes may be exposed."""


def _require(condition, status="protocol_error"):
    if not condition:
        raise _Failure(status)


def _mac(key, data):
    signer = hmac.HMAC(key, hashes.SHA256())
    signer.update(data)
    return signer.finalize()


class _Session:
    def __init__(self, token, app_random, dev_random):
        material = HKDF(algorithm=hashes.SHA256(), length=64,
                        salt=app_random + dev_random,
                        info=b"mible-login-info").derive(token)
        self.dev_key, self.app_key = material[:16], material[16:32]
        self.dev_iv, self.app_iv = material[32:36], material[36:40]
        self.app_confirmation = _mac(self.app_key, app_random + dev_random)
        self.expected = _mac(self.dev_key, dev_random + app_random)
        self.tx_counter = 0
        self.rx_counter = -1

    def verify(self, confirmation):
        if not constant_time.bytes_eq(self.expected, confirmation):
            raise _Failure("auth_failed")

    @staticmethod
    def nonce(iv, counter):
        return iv + b"\x00" * 4 + struct.pack("<I", counter)

    def encrypt(self, plaintext):
        _require(self.tx_counter <= 65535, "counter_exhausted")
        counter = self.tx_counter
        packet = struct.pack("<H", counter) + AESCCM(self.app_key, tag_length=4).encrypt(
            self.nonce(self.app_iv, counter), plaintext, None)
        self.tx_counter += 1
        return packet

    def decrypt(self, packet):
        _require(len(packet) >= 6)
        counter = int.from_bytes(packet[:2], "little")
        _require(counter > self.rx_counter, "replay_rejected")
        try:
            plaintext = AESCCM(self.dev_key, tag_length=4).decrypt(
                self.nonce(self.dev_iv, counter), packet[2:], None)
        except InvalidTag:
            raise _Failure("integrity_error") from None
        self.rx_counter = counter
        return plaintext


class _Channel:
    def __init__(self):
        self.rx_type = None
        self.rx_count = 0
        self.rx_deadline = None
        self.rx_slots = {}
        self.rx_attempts = {}
        self.rx_requested = None
        self.tx_data = None
        self.tx_phase = None
        self.tx_attempts = {}

    @property
    def busy(self):
        return self.rx_type is not None or self.tx_phase is not None


class _Collector:
    def __init__(self, client, on_records, listen_seconds=0):
        self.client, self.on_records = client, on_records
        self.listen_seconds = listen_seconds
        self.queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.overflow = False
        self.notification_counts = {}
        self.subscribed = []
        self.channels = {uuid: _Channel() for uuid in (AUTH_DATA, SPEC_RX, SPEC_TX)}
        self.messages = deque()
        self.confirmation_sent = False
        self.login_success = False
        self.session = None
        self.seen = set()
        self.unknown = False
        self.summary = dict(status="incomplete", phase="starting", authenticated=False,
                            expected_records=None, received_records=0,
                            online_records=0, saved_records=0, duplicate_records=0,
                            complete=False, repair_requests=0)

    async def write(self, uuid, data):
        _require(not self.overflow, "queue_overflow")
        await self.client.write_gatt_char(uuid, data, response=False)

    async def subscribe(self, uuid):
        def callback(_sender, data):
            self.notification_counts[uuid] = self.notification_counts.get(uuid, 0) + 1
            # Bleak dispatches notifications on its asyncio loop. Never spawn
            # per-event tasks: ordering and queue overflow must remain explicit.
            if len(data) > 20:
                self.overflow = True
                return
            try:
                self.queue.put_nowait((uuid, bytes(data)))
            except asyncio.QueueFull:
                self.overflow = True
        await self.client.start_notify(uuid, callback)
        self.subscribed.append(uuid)

    async def request_missing(self, uuid, channel):
        # Public mible_rxfer.c: find_lost_sn / rxfer_rx_ack(A_LOST).
        # Scan the WHOLE CMD range, including a tail never received. Exactly
        # one u16 sequence per six-byte ACK; this is not a window protocol.
        missing = next(sn for sn in range(1, channel.rx_count + 1)
                       if sn not in channel.rx_slots)
        attempts = channel.rx_attempts.get(missing, 0)
        if attempts >= MAX_REPAIR_ATTEMPTS:
            raise TimeoutError
        channel.rx_attempts[missing] = attempts + 1
        channel.rx_requested = missing
        channel.rx_deadline = asyncio.get_running_loop().time() + RECEIVE_IDLE_TIMEOUT
        self.summary["repair_requests"] += 1
        await self.write(uuid, struct.pack("<HBBH", 0, 1, 5, missing))

    async def pump(self, timeout=OPERATION_TIMEOUT):
        _require(not self.overflow, "queue_overflow")
        now = asyncio.get_running_loop().time()
        for channel in self.channels.values():
            if channel.rx_deadline is not None:
                timeout = min(timeout, max(0, channel.rx_deadline - now))
        try:
            async with asyncio.timeout(timeout):
                uuid, frame = await self.queue.get()
        except TimeoutError:
            _require(not self.overflow, "queue_overflow")
            now = asyncio.get_running_loop().time()
            repaired = False
            for uuid, channel in self.channels.items():
                if channel.rx_deadline is not None and channel.rx_deadline <= now:
                    await self.request_missing(uuid, channel)
                    repaired = True
            if repaired:
                return  # Repair idle is NOT run()'s quiet completion timeout.
            raise
        _require(not self.overflow, "queue_overflow")
        if uuid == AUTH_CONTROL:
            _require(len(frame) == 4, "auth_failed")
            _require(frame == b"\x21\x00\x00\x00" and self.confirmation_sent
                     and not self.login_success, "auth_failed")
            self.login_success = True
            return
        channel = self.channels[uuid]
        _require(2 <= len(frame) <= 20)
        sn = int.from_bytes(frame[:2], "little")
        if sn == 0:
            _require(len(frame) >= 4)
            mode, kind = frame[2:4]
            if mode == 1:
                _require(channel.tx_phase is not None)
                if kind == 5:
                    _require(len(frame) == 6 and channel.tx_phase == "success")
                    missing = int.from_bytes(frame[4:6], "little")
                    _require(1 <= missing <= (len(channel.tx_data) + 17) // 18)
                    attempts = channel.tx_attempts.get(missing, 0)
                    _require(attempts < MAX_REPAIR_ATTEMPTS, "transport_error")
                    channel.tx_attempts[missing] = attempts + 1
                    offset = (missing - 1) * 18
                    await self.write(uuid, struct.pack("<H", missing)
                                     + channel.tx_data[offset:offset + 18])
                    return
                _require(len(frame) == 4)
                if kind == 1 and channel.tx_phase == "ready":
                    channel.tx_phase = "success"
                    for offset in range(0, len(channel.tx_data), 18):
                        await self.write(uuid, struct.pack("<H", offset // 18 + 1)
                                         + channel.tx_data[offset:offset + 18])
                    if uuid == AUTH_DATA and self.session is not None:
                        self.confirmation_sent = True
                elif kind == 0 and channel.tx_phase == "success":
                    channel.tx_phase = None
                    channel.tx_data = None
                else:
                    raise _Failure("transport_error")
                return
            _require(mode == 0 and len(frame) == 6 and channel.rx_type is None)
            count = int.from_bytes(frame[4:6], "little")
            _require(1 <= count <= (MAX_PACKET + 17) // 18)
            _require(kind in (0x0D, 0x0C) if uuid == AUTH_DATA else kind == 0)
            channel.rx_type, channel.rx_count = kind, count
            channel.rx_deadline = asyncio.get_running_loop().time() + RECEIVE_IDLE_TIMEOUT
            channel.rx_slots.clear()
            channel.rx_attempts.clear()
            channel.rx_requested = None
            await self.write(uuid, ACK_READY)
            return
        _require(1 <= sn <= channel.rx_count and len(frame) > 2)
        _require(sn == channel.rx_count or len(frame) == 20)
        payload = frame[2:]
        if sn in channel.rx_slots:
            _require(channel.rx_slots[sn] == payload)
            return  # Identical duplicates do not refresh idle or save twice.
        _require(channel.rx_type is not None)
        channel.rx_slots[sn] = payload
        channel.rx_deadline = asyncio.get_running_loop().time() + RECEIVE_IDLE_TIMEOUT
        _require(sum(map(len, channel.rx_slots.values())) <= MAX_PACKET)
        if len(channel.rx_slots) == channel.rx_count:
            _require(len(self.messages) < 64, "queue_overflow")
            packet = b"".join(channel.rx_slots[i] for i in range(1, channel.rx_count + 1))
            self.messages.append((uuid, channel.rx_type, packet))
            channel.rx_type = None
            channel.rx_deadline = None
            await self.write(uuid, ACK_SUCCESS)
        elif sn == channel.rx_requested:
            await self.request_missing(uuid, channel)

    async def send(self, uuid, kind, packet):
        channel = self.channels[uuid]
        _require(channel.tx_phase is None and 1 <= len(packet) <= MAX_PACKET)
        channel.tx_data, channel.tx_phase = packet, "ready"
        channel.tx_attempts.clear()
        await self.write(uuid, struct.pack("<HBBH", 0, 0, kind, (len(packet) + 17) // 18))
        # One absolute deadline for the whole transfer, not per fragment.
        async with asyncio.timeout(OPERATION_TIMEOUT):
            while channel.tx_phase is not None:
                await self.pump()

    async def receive_auth(self, kind, length):
        async with asyncio.timeout(OPERATION_TIMEOUT):
            while not self.messages:
                await self.pump()
        uuid, received_kind, data = self.messages.popleft()
        _require(uuid == AUTH_DATA and received_kind == kind and len(data) == length,
                 "auth_failed")
        return data

    async def login(self, token):
        self.summary["phase"] = "login_start"
        await self.subscribe(AUTH_CONTROL)
        await self.subscribe(AUTH_DATA)
        await self.write(AUTH_CONTROL, b"\x24\x00\x00\x00")
        app_random = secrets.token_bytes(16)
        self.summary["phase"] = "app_random"
        await self.send(AUTH_DATA, 0x0B, app_random)
        self.summary["phase"] = "device_random"
        dev_random = await self.receive_auth(0x0D, 16)
        self.session = _Session(token, app_random, dev_random)
        self.summary["phase"] = "device_confirmation"
        confirmation = await self.receive_auth(0x0C, 32)
        self.session.verify(confirmation)
        self.summary["phase"] = "app_confirmation"
        await self.send(AUTH_DATA, 0x0A, self.session.app_confirmation)
        async with asyncio.timeout(OPERATION_TIMEOUT):
            while not self.login_success:
                await self.pump()
        self.summary["authenticated"] = True

    async def handle_spec(self, packet):
        frame = self.session.decrypt(packet)
        _require(len(frame) >= 5)
        header, tid, opcode = struct.unpack_from("<HHB", frame)
        _require(header >> 12 == 2 and header & 4095 == len(frame))
        if opcode == 6:
            # Evidence: spec-response-codecs.json fs4.OooO0OO: u16 result,
            # optional u8 count, then v2 (u16 PIID,u16 type_len,value) TLVs.
            # No SIID/AIID in this response; correlate to our sole request tid=1.
            if tid != 1:
                self.unknown = True
                return
            _require(len(frame) >= 7)
            _require(int.from_bytes(frame[5:7], "little") == 0, "action_failed")
            args = _arguments(frame, 8, frame[7]) if len(frame) >= 8 else []
            counts = [(kind, value) for piid, kind, value in args if piid == 3]
            if (len(counts) != 1 or counts[0][0] not in range(1, 9)
                    or counts[0][1] < 0):
                self.unknown = True
                return
            count = counts[0][1]
            expected = self.summary["expected_records"]
            _require(expected is None or expected == count)
            self.summary["expected_records"] = count
            return
        if opcode != 7:
            self.unknown = True
            return
        _require(len(frame) >= 9)
        siid, eiid, count = struct.unpack_from("<BHB", frame, 5)
        args = _arguments(frame, 9, count)
        rule = {(9, 1): (1, True), (10, 4): (4, False)}.get((siid, eiid))
        if rule is None:
            return
        piid, offline = rule
        _require(len(args) == 1 and args[0][:2] == (piid, 10))
        csvs = args[0][2].split("_") if offline else [args[0][2]]
        # Lazy import keeps the BLE codec independent of HA and allows a caller
        # to import it while the pure history module is being initialized.
        from .scale_history import parse_native_csv
        records = [parse_native_csv(csv, offline=offline) for csv in csvs]
        try:
            await self.on_records(records, source="gatt_offline" if offline else "gatt_online")
        except asyncio.CancelledError:
            raise
        except Exception:
            raise _Failure("save_error") from None
        self.summary["saved_records"] += len(records)
        if not offline:
            self.summary["online_records"] += len(records)
            return
        for csv in csvs:
            identity = hashlib.sha256(csv.encode("utf-8")).digest()
            if identity in self.seen:
                self.summary["duplicate_records"] += 1
            else:
                self.seen.add(identity)
                self.summary["received_records"] += 1

    async def run(self, token):
        await self.login(token)
        await self.subscribe(SPEC_RX)
        await self.subscribe(SPEC_TX)
        plaintext = struct.pack("<HHBBBB", 0x2008, 1, 5, 9, 1, 0)
        self.summary["phase"] = "history_request"
        await self.send(SPEC_RX, 0, self.session.encrypt(plaintext))
        self.summary["phase"] = "history_receive"
        listen_until = asyncio.get_running_loop().time() + self.listen_seconds
        while True:
            while self.messages:
                uuid, kind, packet = self.messages.popleft()
                _require(uuid in (SPEC_RX, SPEC_TX) and kind == 0)
                await self.handle_spec(packet)
            busy = any(channel.busy for channel in self.channels.values())
            try:
                await self.pump(OPERATION_TIMEOUT if busy else QUIET_TIMEOUT)
            except TimeoutError:
                _require(not self.overflow, "queue_overflow")
                if busy:
                    raise
                if asyncio.get_running_loop().time() < listen_until:
                    continue
                break
        expected = self.summary["expected_records"]
        complete = (expected is not None and self.summary["received_records"] == expected
                    and not self.unknown and not self.overflow)
        self.summary.update(complete=complete, status="complete" if complete else "incomplete")


def _arguments(frame, offset, count):
    args = []
    formats = {0: "<?", 1: "<B", 2: "<b", 3: "<H", 4: "<h", 5: "<I",
               6: "<i", 7: "<Q", 8: "<q", 9: "<f"}
    for _ in range(count):
        _require(offset + 4 <= len(frame))
        piid, descriptor = struct.unpack_from("<HH", frame, offset)
        offset += 4
        kind, size = descriptor >> 12, descriptor & 4095
        _require(offset + size <= len(frame))
        payload = frame[offset:offset + size]
        offset += size
        if kind == 10:
            value = payload.decode("utf-8", errors="strict")
        else:
            fmt = formats.get(kind)
            _require(fmt is not None and struct.calcsize(fmt) == size)
            value = struct.unpack(fmt, payload)[0]
        args.append((piid, kind, value))
    _require(offset == len(frame))
    return args


async def collect_history(client, token: bytes,
                          on_records: Callable[..., Awaitable[object]],
                          timeout: float = 75, listen_seconds: float = 0) -> dict:
    """Collect on an already connected Bleak-compatible client; never disconnect.

    await on_records(list[parse_native_csv result], source='gatt_offline' or
    'gatt_online') must finish durable saving or raise. Every valid batch,
    including duplicate history, is delivered. Counts advance only after it
    returns. received_records counts unique *exact offline CSVs including idx*
    in THIS session; online_records counts saved online observations, and
    saved_records includes repeats. expected_records is None until a validated
    successful tid=1 opcode6 response supplies integer PIID3. Completion requires
    exact offline count equality plus an idle quiet period, never login alone.

    repair_requests counts host ACK_LOST requests (one sequence each, at most
    three attempts per missing sequence). Receive idle is a local 0.4s policy;
    auth and SPEC both repair missing tails without requiring the final frame.
    Returns only statuses/booleans/counts; protocol, integrity, save and IO errors
    stop immediately with complete=False, retaining previously saved counts.
    Caller cancellation propagates. Finally stops all successfully enabled
    notifications; cleanup failures force notification_cleanup_failed. No keys,
    CSVs, exception strings, user IDs or weights appear in the summary.
    """
    if not isinstance(token, bytes) or len(token) != 12:
        raise ValueError("A 12-byte BLE token is required")
    if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("A positive finite timeout is required")
    if (isinstance(listen_seconds, bool) or not math.isfinite(listen_seconds)
            or not 0 <= listen_seconds < timeout):
        raise ValueError("Listen window must fit inside the total timeout")
    collector = _Collector(client, on_records, listen_seconds)
    try:
        async with asyncio.timeout(timeout):
            await collector.run(token)
    except TimeoutError:
        collector.summary.update(status="queue_overflow" if collector.overflow else "timeout",
                                 complete=False)
    except _Failure as error:
        collector.summary.update(status=str(error), complete=False)
    except (ValueError, UnicodeError, struct.error):
        collector.summary.update(status="protocol_error", complete=False)
    except Exception:
        collector.summary.update(status="io_error", complete=False)
    finally:
        for uuid in reversed(collector.subscribed):
            try:
                async with asyncio.timeout(OPERATION_TIMEOUT):
                    await client.stop_notify(uuid)
            except Exception:
                collector.summary.update(status="notification_cleanup_failed", complete=False)
        if not collector.summary["complete"]:
            collector.summary["transport"] = {
                uuid[:8]: {"notifications": collector.notification_counts.get(uuid, 0),
                           "receiving": channel.rx_type is not None,
                           "received_frames": len(channel.rx_slots),
                           "expected_frames": channel.rx_count,
                           "repair_requests": sum(channel.rx_attempts.values()),
                           "pending_bytes": (sum(map(len, channel.rx_slots.values()))
                                             if channel.rx_type is not None else 0),
                           "sending": channel.tx_phase}
                for uuid, channel in collector.channels.items()}
        collector.session = None
    return collector.summary

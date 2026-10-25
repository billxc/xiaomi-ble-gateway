"""Entirely synthetic async Bleak peer; no adapters, captures or real secrets."""
import asyncio
from collections import defaultdict, deque
import hashlib
import hmac
import importlib.util
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_DIRECTORY = Path(__file__).resolve().parents[1] / "custom_components/xiaomi_ble_gateway"
# Isolated package permits the real relative scale_history import without HA's
# integration __init__ and without replacing modules used by other test files.
_PACKAGE = "_synthetic_gatt_tests"
_package = types.ModuleType(_PACKAGE)
_package.__path__ = [str(_DIRECTORY)]
sys.modules[_PACKAGE] = _package
_spec = importlib.util.spec_from_file_location(_PACKAGE + ".scale_gatt", _DIRECTORY / "scale_gatt.py")
gatt = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gatt
_spec.loader.exec_module(gatt)
TOKEN = b"test-token12"  # synthetic, not metadata
DEV_RANDOM = bytes(range(16))
CSV = ",".join(["123456", "17", "1", "7012", "72", "0", "1800000000"] + ["1"] * 27)
OFFLINE = "11," + CSV


def frame(opcode, body, tid=1):
    return struct.pack("<HHB", 0x2000 | (5 + len(body)), tid, opcode) + body


def argument(piid, kind, data):
    return struct.pack("<HH", piid, kind << 12 | len(data)) + data


def count_response(count, kind=5, tid=1, result=0):
    formats = {1: "<B", 2: "<b", 3: "<H", 4: "<h", 5: "<I", 6: "<i",
               7: "<Q", 8: "<q", 9: "<f"}
    data = str(count).encode() if kind == 10 else struct.pack(formats[kind], count)
    return frame(6, struct.pack("<HB", result, 1) + argument(3, kind, data), tid)


def event(csv, offline=True):
    return frame(7, struct.pack("<BHB", 9 if offline else 10, 1 if offline else 4, 1)
                 + argument(1 if offline else 4, 10, csv.encode()))


class FakeBleak:
    """Independent device side of classic reliable channel and Standard Auth."""
    def __init__(self, plaintexts=(), *, bad_confirmation=False, login_code=0x21,
                 tamper=False, disorder=False, counters=None, stall=False,
                 overflow=False, drop_fragment=False, response_on_rx=False):
        self.callbacks = {}
        self.stopped = []
        self.writes = []
        self.inbound = {}
        self.outbound = defaultdict(deque)
        self.plaintexts = plaintexts
        self.bad_confirmation, self.login_code = bad_confirmation, login_code
        self.tamper, self.disorder, self.counters = tamper, disorder, counters
        self.stall, self.overflow = stall, overflow
        self.drop_fragment, self.response_on_rx = drop_fragment, response_on_rx
        self.actions = []
        self.auth_kinds = []
        self.app_confirmation_received = False
        self.material = None

    async def start_notify(self, uuid, callback):
        if uuid == gatt.SPEC_RX:
            assert self.app_confirmation_received
        self.callbacks[uuid] = callback

    async def stop_notify(self, uuid):
        self.stopped.append(uuid)
        self.callbacks.pop(uuid)

    def notify(self, uuid, data):
        self.callbacks[uuid](object(), bytearray(data))

    def outgoing(self, uuid, kind, data, *, disorder=False, drop=False):
        pending = self.outbound[uuid]
        pending.append((kind, data, disorder, drop))
        if len(pending) == 1:
            self.command(uuid)

    def command(self, uuid):
        kind, data, _, _ = self.outbound[uuid][0]
        self.notify(uuid, struct.pack("<HBBH", 0, 0, kind, (len(data) + 17) // 18))

    async def write_gatt_char(self, uuid, data, *, response):
        assert response is False
        data = bytes(data)
        self.writes.append((uuid, data))
        if uuid == gatt.AUTH_CONTROL:
            assert data == b"\x24\0\0\0"
            assert set(self.callbacks) == {gatt.AUTH_CONTROL, gatt.AUTH_DATA}
            return
        if data[:4] == b"\0\0\x01\x05":
            assert len(data) == 6  # Default peer never repairs dropped frames.
            return
        if data == gatt.ACK_READY:
            _, packet, disorder, drop = self.outbound[uuid][0]
            parts = [struct.pack("<H", pos // 18 + 1) + packet[pos:pos + 18]
                     for pos in range(0, len(packet), 18)]
            if disorder:
                parts[0], parts[1] = parts[1], parts[0]
            if drop:
                parts = parts[:-1]
            for part in parts:
                self.notify(uuid, part)
            return
        if data == gatt.ACK_SUCCESS:
            self.outbound[uuid].popleft()
            if self.outbound[uuid]:
                self.command(uuid)
            return
        sn = int.from_bytes(data[:2], "little")
        if sn == 0:
            _, mode, kind, count = struct.unpack("<HBBH", data)
            assert mode == 0
            self.inbound[uuid] = [kind, count, bytearray(), 1]
            if uuid == gatt.AUTH_DATA:
                self.auth_kinds.append(kind)
            if not self.stall:
                self.notify(uuid, gatt.ACK_READY)
            return
        incoming = self.inbound[uuid]
        assert incoming[3] == sn
        incoming[3] += 1
        incoming[2].extend(data[2:])
        if sn != incoming[1]:
            return
        kind, _, packet, _ = incoming
        packet = bytes(packet)
        self.notify(uuid, gatt.ACK_SUCCESS)
        if uuid == gatt.AUTH_DATA and kind == 0x0B:
            assert len(packet) == 16
            self.app_random = packet
            self.material = HKDF(algorithm=hashes.SHA256(), length=64,
                                 salt=packet + DEV_RANDOM, info=b"mible-login-info").derive(TOKEN)
            confirmation = hmac.new(self.material[:16], DEV_RANDOM + packet, hashlib.sha256).digest()
            if self.bad_confirmation:
                confirmation = bytes([confirmation[0] ^ 1]) + confirmation[1:]
            self.outgoing(uuid, 0x0D, DEV_RANDOM)
            self.outgoing(uuid, 0x0C, confirmation)
        elif uuid == gatt.AUTH_DATA and kind == 0x0A:
            expected = hmac.new(self.material[16:32], self.app_random + DEV_RANDOM,
                                hashlib.sha256).digest()
            assert hmac.compare_digest(packet, expected)
            self.app_confirmation_received = True
            if self.login_code is not None:
                self.notify(gatt.AUTH_CONTROL, struct.pack("<I", self.login_code))
        else:
            assert uuid == gatt.SPEC_RX and kind == 0
            counter = int.from_bytes(packet[:2], "little")
            nonce = self.material[36:40] + b"\0" * 4 + struct.pack("<I", counter)
            plain = AESCCM(self.material[16:32], tag_length=4).decrypt(nonce, packet[2:], None)
            self.actions.append(plain)
            assert plain == bytes.fromhex("0820010005090100")
            if self.overflow:
                for _ in range(gatt.QUEUE_SIZE + 1):
                    self.notify(gatt.SPEC_TX, gatt.ACK_READY)
                return
            for index, plaintext in enumerate(self.plaintexts):
                seq = self.counters[index] if self.counters is not None else index
                nonce = self.material[32:36] + b"\0" * 4 + struct.pack("<I", seq)
                encrypted = AESCCM(self.material[:16], tag_length=4).encrypt(nonce, plaintext, None)
                if self.tamper and index == len(self.plaintexts) - 1:
                    encrypted = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
                char = gatt.SPEC_RX if self.response_on_rx else gatt.SPEC_TX
                self.outgoing(char, 0, struct.pack("<H", seq) + encrypted,
                              disorder=self.disorder, drop=self.drop_fragment)


class TailRepairBleak(FakeBleak):
    """Only an explicit single-sequence ACK_LOST releases each missing tail."""
    def __init__(self, *args, respond=True, duplicate=False, conflict=False,
                 repair_auth=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.respond, self.duplicate, self.conflict = respond, duplicate, conflict
        self.repair_auth = repair_auth
        self.repairs = []

    async def write_gatt_char(self, uuid, data, *, response):
        data = bytes(data)
        if data[:4] == b"\0\0\x01\x05":
            assert response is False and len(data) == 6
            self.writes.append((uuid, data))
            seq = int.from_bytes(data[4:], "little")
            _, packet, _, _ = self.outbound[uuid][0]
            assert 1 <= seq <= (len(packet) + 17) // 18
            self.repairs.append((uuid, seq))
            if self.respond:
                offset = (seq - 1) * 18
                self.notify(uuid, struct.pack("<H", seq) + packet[offset:offset + 18])
            return
        if data == gatt.ACK_READY:
            kind, packet, _, _ = self.outbound[uuid][0]
            count = (len(packet) + 17) // 18
            if count == 29 or (self.repair_auth and kind == 0x0C):
                self.writes.append((uuid, data))
                initial = 17 if count == 29 else 1
                for seq in range(1, initial + 1):
                    offset = (seq - 1) * 18
                    part = struct.pack("<H", seq) + packet[offset:offset + 18]
                    self.notify(uuid, part)
                    if seq == 3 and (self.duplicate or self.conflict):
                        duplicate = (part[:-1] + bytes([part[-1] ^ 1])
                                     if self.conflict else part)
                        self.notify(uuid, duplicate)
                return
        await super().write_gatt_char(uuid, data, response=response)


class ScaleGattTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.saved = []
        self.quiet_patch = patch.object(gatt, "QUIET_TIMEOUT", 0.01)
        self.quiet_patch.start()
        self.addCleanup(self.quiet_patch.stop)

    async def save(self, records, *, source):
        await asyncio.sleep(0)  # async persistence backpressure is deliberate
        self.saved.append((records, source))

    async def collect(self, device, **kwargs):
        result = await gatt.collect_history(device, TOKEN, self.save, timeout=kwargs.pop("timeout", 0.3), **kwargs)
        self.assertEqual(set(device.callbacks), set())
        self.assertEqual(len(device.stopped), len(set(device.stopped)))
        self.assertTrue(set(result) <= {"status", "phase", "transport", "authenticated", "expected_records", "received_records",
                                      "online_records", "saved_records", "duplicate_records", "complete", "repair_requests"})
        return result

    async def test_upload_listen_window_does_not_end_at_first_quiet_gap(self):
        device = FakeBleak([count_response(0)])
        started = asyncio.get_running_loop().time()
        result = await self.collect(device, listen_seconds=0.03)
        self.assertGreaterEqual(asyncio.get_running_loop().time() - started, 0.03)
        self.assertTrue(result["complete"])
        self.assertEqual(len(device.actions), 1)
        self.assertEqual(self.saved, [])

    async def test_full_login_history_duplicates_and_online_actual_csv_parser(self):
        device = FakeBleak([count_response(2), event(OFFLINE + "_" + OFFLINE),
                            event(CSV, offline=False), event("12," + CSV)])
        with patch.object(gatt.secrets, "token_bytes", return_value=b"A" * 16) as random:
            result = await self.collect(device)
        random.assert_called_once_with(16)
        self.assertEqual(result, dict(status="complete", phase="history_receive", authenticated=True, expected_records=2,
                                     received_records=2, online_records=1, saved_records=4,
                                     duplicate_records=1, complete=True, repair_requests=0))
        self.assertEqual(device.auth_kinds, [0x0B, 0x0A])
        self.assertEqual(len(device.actions), 1)
        self.assertEqual([source for _, source in self.saved], ["gatt_offline", "gatt_online", "gatt_offline"])
        parsed = self.saved[0][0][0]
        self.assertEqual(len(parsed["raw_fields"]), 34)
        self.assertEqual(parsed["idx"], "11")
        self.assertEqual(parsed["data"]["weight"], "70.12")
        self.assertEqual(parsed["data"]["miid"], "123456")
        # Multi-fragment offline event is not delivered until ACK_SUCCESS.
        self.assertIn((gatt.SPEC_TX, gatt.ACK_READY), device.writes)
        self.assertIn((gatt.SPEC_TX, gatt.ACK_SUCCESS), device.writes)

    async def test_bad_device_confirmation_is_fail_closed(self):
        device = FakeBleak(bad_confirmation=True)
        result = await self.collect(device)
        self.assertEqual(result["status"], "auth_failed")
        self.assertFalse(result["authenticated"])
        self.assertEqual(device.auth_kinds, [0x0B])
        self.assertFalse(device.app_confirmation_received)
        self.assertEqual(device.actions, [])
        self.assertEqual(self.saved, [])

    async def test_login_control_failure_and_missing_control_success(self):
        for code in (0x22, 0x23, 0xE0, 0xE1, 0xE2, None):
            with self.subTest(code=code):
                device = FakeBleak(login_code=code)
                result = await self.collect(device, timeout=0.03)
                self.assertEqual(result["status"], "timeout" if code is None else "auth_failed")
                self.assertFalse(result["authenticated"])
                self.assertFalse(device.actions)

    async def test_early_success_is_not_a_transport_ack_or_valid_login(self):
        class EarlySuccess(FakeBleak):
            async def write_gatt_char(self, uuid, data, **kwargs):
                await super().write_gatt_char(uuid, data, **kwargs)
                if uuid == gatt.AUTH_CONTROL:
                    self.notify(uuid, b"\x21\0\0\0")
        result = await self.collect(EarlySuccess())
        self.assertEqual(result["status"], "auth_failed")
        self.assertFalse(result["authenticated"])

    async def test_ccm_tamper_retains_only_previously_saved_history(self):
        result = await self.collect(FakeBleak([event(OFFLINE), count_response(1)], tamper=True))
        self.assertEqual(result["status"], "integrity_error")
        self.assertFalse(result["complete"])
        self.assertEqual(result["received_records"], 1)
        self.assertEqual(len(self.saved), 1)

    async def test_spec_fragment_out_of_order_reassembled_or_timeout_if_missing(self):
        result = await self.collect(FakeBleak([count_response(1), event(OFFLINE)], disorder=True))
        self.assertTrue(result["complete"])
        self.assertEqual(len(self.saved), 1)
        self.saved.clear()
        with patch.object(gatt, "RECEIVE_IDLE_TIMEOUT", 0.002):
            result = await self.collect(FakeBleak([event(OFFLINE)], disorder=True, drop_fragment=True))
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(result["repair_requests"], 3)
        self.assertEqual(self.saved, [])

    async def test_counter_repeat_and_backwards_rejected(self):
        for counters in ([0, 0], [4, 3], [65535, 0]):
            with self.subTest(counters=counters):
                self.saved.clear()
                result = await self.collect(FakeBleak([event(OFFLINE), event("12," + CSV)], counters=counters))
                self.assertEqual(result["status"], "replay_rejected")
                self.assertEqual(result["received_records"], 1)
                self.assertEqual(len(self.saved), 1)

    async def test_both_spec_characteristics_receive_and_ack(self):
        device = FakeBleak([count_response(1), event(OFFLINE)], response_on_rx=True)
        result = await self.collect(device)
        self.assertTrue(result["complete"])
        self.assertIn((gatt.SPEC_RX, gatt.ACK_SUCCESS), device.writes)
        self.assertNotIn((gatt.SPEC_TX, gatt.ACK_SUCCESS), device.writes)

    async def test_no_count_or_count_mismatch_cannot_claim_complete(self):
        for packets in ([event(OFFLINE)], [count_response(2), event(OFFLINE)],
                        [count_response(1), event(OFFLINE + "_" + OFFLINE), event(OFFLINE)],
                        [count_response(0), event(OFFLINE)]):
            with self.subTest(packets=len(packets)):
                result = await self.collect(FakeBleak(packets))
                # Duplicates cannot satisfy expected=2; expected=1 may complete.
                if len(packets) == 3:
                    self.assertTrue(result["complete"])
                    self.assertEqual(result["received_records"], 1)
                    self.assertEqual(result["duplicate_records"], 2)
                else:
                    self.assertEqual(result["status"], "incomplete")
                    self.assertFalse(result["complete"])

    async def test_count_formats_zero_and_unknown_responses(self):
        for kind in range(1, 9):
            with self.subTest(kind=kind):
                result = await self.collect(FakeBleak([count_response(0, kind)]))
                self.assertTrue(result["complete"])
                self.assertEqual(result["expected_records"], 0)
        for packet in (count_response(1, 10), count_response(1, 9),
                       count_response(-1, 6), count_response(0, tid=2),
                       frame(6, b"\0\0"), frame(0xF0, b"")):
            with self.subTest(packet=packet.hex()):
                result = await self.collect(FakeBleak([packet]))
                self.assertEqual(result["status"], "incomplete")
                self.assertIsNone(result["expected_records"])
        result = await self.collect(FakeBleak([count_response(0, result=1)]))
        self.assertEqual(result["status"], "action_failed")

    async def test_incomplete_fragment_and_transfer_ack_timeout(self):
        for device in (FakeBleak([event(OFFLINE)], drop_fragment=True), FakeBleak(stall=True)):
            with self.subTest(stall=device.stall):
                result = await self.collect(device, timeout=0.03)
                self.assertEqual(result["status"], "timeout")
                self.assertFalse(result["complete"])
                self.assertEqual(self.saved, [])

    async def test_queue_overflow_is_explicit_failure(self):
        result = await self.collect(FakeBleak(overflow=True))
        self.assertEqual(result["status"], "queue_overflow")
        self.assertEqual(result["received_records"], 0)
        self.assertFalse(result["complete"])

    async def test_save_failure_never_increments_unsaved_batch(self):
        async def save(records, *, source):
            if self.saved:
                raise RuntimeError("synthetic private CSV must never be returned")
            await self.save(records, source=source)
        device = FakeBleak([event(OFFLINE), event("12," + CSV)])
        result = await gatt.collect_history(device, TOKEN, save, timeout=0.3)
        self.assertEqual(result["status"], "save_error")
        self.assertEqual(result["saved_records"], 1)
        self.assertEqual(result["received_records"], 1)
        self.assertNotIn("private", str(result))
        self.assertFalse(device.callbacks)

    async def test_invalid_csv_and_spec_length_never_delivered(self):
        for packet in (event("1,2,3"), event("11," + CSV + "_"),
                       b"\x00\x20" + event(OFFLINE)[2:],
                       frame(7, b"\x09\x01\x00\x01" + argument(1, 10, b"\xff"))):
            with self.subTest(packet_size=len(packet)):
                result = await self.collect(FakeBleak([packet]))
                self.assertEqual(result["status"], "protocol_error")
                self.assertEqual(self.saved, [])

    async def test_cancellation_propagates_and_stops_notify(self):
        device = FakeBleak(stall=True)
        task = asyncio.create_task(gatt.collect_history(device, TOKEN, self.save))
        await asyncio.sleep(0.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(device.callbacks)
        self.assertEqual(set(device.stopped), {gatt.AUTH_CONTROL, gatt.AUTH_DATA})

    async def test_token_and_timeout_validation_no_io(self):
        for token in (b"short", b"a" * 16, "a" * 12):
            device = FakeBleak()
            with self.assertRaises(ValueError):
                await gatt.collect_history(device, token, self.save)
            self.assertEqual(device.writes, [])
        for timeout in (0, -1, True, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                await gatt.collect_history(FakeBleak(), TOKEN, self.save, timeout=timeout)

    async def test_invalid_ack_rejected_and_auth_fragment_order_supported(self):
        class BadAck(FakeBleak):
            async def write_gatt_char(self, uuid, data, **kwargs):
                if uuid == gatt.AUTH_DATA and bytes(data)[2:4] == b"\0\x0b":
                    self.notify(uuid, gatt.ACK_SUCCESS)  # not READY
                    return
                await super().write_gatt_char(uuid, data, **kwargs)
        result = await self.collect(BadAck())
        self.assertEqual(result["status"], "transport_error")
        self.assertFalse(result["authenticated"])

        class BadConfirmationOrder(FakeBleak):
            def outgoing(self, uuid, kind, data, **kwargs):
                super().outgoing(uuid, kind, data, disorder=kind == 0x0C)
        device = BadConfirmationOrder()
        result = await self.collect(device)
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["authenticated"])
        self.assertEqual(device.auth_kinds, [0x0B, 0x0A])
        self.assertEqual(len(device.actions), 1)

    async def test_partial_receive_has_operation_deadline_not_quiet_success(self):
        with patch.object(gatt, "OPERATION_TIMEOUT", 0.02):
            result = await self.collect(FakeBleak([event(OFFLINE)], drop_fragment=True))
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["authenticated"])
        self.assertFalse(result["complete"])

    async def test_duplicate_count_identity_and_conflicting_count_are_not_success(self):
        duplicate = frame(6, b"\0\0\x02" + argument(3, 1, b"\0")
                          + argument(3, 10, b"0"))
        result = await self.collect(FakeBleak([duplicate]))
        self.assertEqual(result["status"], "incomplete")
        self.assertIsNone(result["expected_records"])
        result = await self.collect(FakeBleak([count_response(0), count_response(1)]))
        self.assertEqual(result["status"], "protocol_error")
        self.assertFalse(result["complete"])

    async def test_cleanup_failure_never_claims_complete_but_attempts_all(self):
        class BadCleanup(FakeBleak):
            async def stop_notify(self, uuid):
                await super().stop_notify(uuid)
                if uuid == gatt.SPEC_TX:
                    raise RuntimeError("synthetic stop failure")
        device = BadCleanup([count_response(0)])
        result = await self.collect(device)
        self.assertEqual(result["status"], "notification_cleanup_failed")
        self.assertEqual(len(device.stopped), 4)
        self.assertFalse(result["complete"])

    def tail_history(self):
        # One strictly valid native 34-field record plus idx, padded with a
        # synthetic integer identifier to make an exact 29-fragment packet.
        fields = CSV.split(",")
        fields[0] += "1" * (515 - (len(event(OFFLINE)) + 6))
        csv = "11," + ",".join(fields)
        self.assertEqual((len(event(csv)) + 6 + 17) // 18, 29)
        return csv

    async def test_repair_tail_18_through_29_then_real_csv_parser_once(self):
        csv = self.tail_history()
        device = TailRepairBleak([count_response(1), event(csv)], repair_auth=True)
        with patch.object(gatt, "RECEIVE_IDLE_TIMEOUT", 0.002):
            result = await self.collect(device)
        self.assertTrue(result["complete"])
        self.assertEqual(device.repairs, [(gatt.AUTH_DATA, 2)]
                         + [(gatt.SPEC_TX, seq) for seq in range(18, 30)])
        self.assertIn((gatt.SPEC_TX, bytes.fromhex("000001051200")), device.writes)
        self.assertEqual(result["repair_requests"], 13)
        self.assertEqual(result["saved_records"], 1)
        self.assertEqual(len(self.saved), 1)
        records, source = self.saved[0]
        self.assertEqual(source, "gatt_offline")
        self.assertEqual(records[0]["raw_fields"], csv.split(",")[1:])
        self.assertEqual(records[0]["data"]["weight"], "70.12")
        self.assertEqual(len(device.actions), 1)

    async def test_repair_identical_duplicate_ignored_conflicting_duplicate_rejected(self):
        csv = self.tail_history()
        for conflict in (False, True):
            with self.subTest(conflict=conflict):
                self.saved.clear()
                device = TailRepairBleak([count_response(1), event(csv)],
                                         duplicate=True, conflict=conflict)
                with patch.object(gatt, "RECEIVE_IDLE_TIMEOUT", 0.002):
                    result = await self.collect(device)
                if conflict:
                    self.assertEqual(result["status"], "protocol_error")
                    self.assertEqual(self.saved, [])
                    self.assertEqual(device.repairs, [])
                else:
                    self.assertTrue(result["complete"])
                    self.assertEqual(result["saved_records"], 1)
                    self.assertEqual(result["duplicate_records"], 0)
                    self.assertEqual(len(self.saved), 1)

    async def test_repair_never_returns_bounded_and_tx_single_fragment_resend(self):
        device = TailRepairBleak([count_response(1), event(self.tail_history())], respond=False)
        with patch.object(gatt, "RECEIVE_IDLE_TIMEOUT", 0.002):
            result = await self.collect(device)
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(device.repairs, [(gatt.SPEC_TX, 18)] * 3)
        self.assertEqual(result["repair_requests"], 3)
        self.assertEqual(result["expected_records"], 1)
        self.assertEqual(result["saved_records"], 0)
        self.assertEqual(self.saved, [])
        self.assertEqual(result["transport"]["0000001b"]["received_frames"], 17)

        class LostAppFragment(FakeBleak):
            requests = 0
            always = False

            async def write_gatt_char(self, uuid, data, *, response):
                if (uuid == gatt.AUTH_DATA and bytes(data)[:2] == b"\x02\0"
                        and self.inbound[uuid][0] == 0x0A
                        and (self.requests == 0 or self.always)):
                    self.requests += 1
                    self.writes.append((uuid, bytes(data)))
                    self.notify(uuid, bytes.fromhex("000001050200"))
                    return
                await super().write_gatt_char(uuid, data, response=response)

        device = LostAppFragment([count_response(0)])
        result = await self.collect(device)
        self.assertTrue(result["complete"])
        self.assertEqual(device.requests, 1)
        device = LostAppFragment()
        device.always = True
        result = await self.collect(device)
        self.assertEqual(result["status"], "transport_error")
        self.assertEqual(device.requests, 4)  # original plus exactly 3 resends
        self.assertFalse(result["authenticated"])

    async def test_tx_counter_nonce_and_no_wrap(self):
        session = gatt._Session(TOKEN, b"A" * 16, DEV_RANDOM)
        session.tx_counter = 65535
        packet = session.encrypt(b"synthetic")
        self.assertEqual(packet[:2], b"\xff\xff")
        self.assertEqual(AESCCM(session.app_key, tag_length=4).decrypt(
            session.app_iv + b"\0" * 4 + b"\xff\xff\0\0", packet[2:], None), b"synthetic")
        with self.assertRaises(gatt._Failure) as raised:
            session.encrypt(b"must not reuse nonce")
        self.assertEqual(str(raised.exception), "counter_exhausted")


if __name__ == "__main__":
    unittest.main()

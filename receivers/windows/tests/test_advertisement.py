"""Offline stdlib tests: python -m unittest discover -s tests -v."""

from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from advertisement import pack_advertisement
from winrt_capture import WindowsScanner


def unpack(data):
    result = []
    while data:
        size = data[0]
        if not size or len(data) < size + 1:
            raise AssertionError("malformed AD")
        result.append((data[1], data[2:size + 1]))
        data = data[size + 1:]
    return result


class AdvertisementTests(unittest.TestCase):
    def test_unknown_types_order_and_duplicates_preserved(self):
        sections = [(0x01, b"\x06"), (0xef, b"\x00\xff\x02"), (0xef, b""), (0x30, b"test")]
        self.assertEqual(unpack(pack_advertisement(sections)), sections)
        self.assertEqual(pack_advertisement([]), b"")

    def test_fe95_uuid_and_every_ciphertext_byte_unchanged(self):
        # No bindkey or MiBeacon parsing: ciphertext must stay opaque.
        payload = bytes.fromhex("95fe5850b99a7f112233445566") + bytes(range(64))
        raw = pack_advertisement([(0x01, b"\x06"), (0x16, payload)])
        self.assertEqual(raw, b"\x02\x01\x06" + bytes((len(payload) + 1, 0x16)) + payload)
        self.assertEqual(unpack(raw)[1], (0x16, payload))

    def test_metadata_manufacturer_uuid_widths_and_entire_uuid_little_endian(self):
        custom = UUID("00112233-4455-6677-8899-aabbccddeeff")
        result = unpack(pack_advertisement(
            [], manufacturer_data={0x1234: b"\xaa\xbb"},
            service_uuids=["fe95", "12345678", custom],
            local_name="测试", flags=6, tx_power=-12,
        ))
        self.assertEqual(result, [
            (0xff, b"\x34\x12\xaa\xbb"),
            (0x02, b"\x95\xfe"),
            (0x04, b"\x78\x56\x34\x12"),
            (0x06, bytes.fromhex("ffeeddccbbaa99887766554433221100")),
            (0x09, "测试".encode()), (0x01, b"\x06"), (0x0a, b"\xf4"),
        ])

    def test_raw_fields_win_no_duplicate_metadata(self):
        custom = UUID("00112233-4455-6677-8899-aabbccddeeff")
        sections = [
            (0x01, b"\x04"), (0xff, b"\x34\x12original"),
            (0x03, b"\x95\xfe"), (0x04, b"\x78\x56\x34\x12"),
            (0x07, custom.bytes[::-1]), (0x08, b"short"), (0x0a, b"\xff"),
        ]
        data = pack_advertisement(
            sections, manufacturer_data=[(0x1234, b"different")],
            service_uuids=["0000fe95-0000-1000-8000-00805f9b34fb", "12345678", custom],
            local_name="longer name", flags=6, tx_power=0,
        )
        self.assertEqual(unpack(data), sections)

    def test_service_data_uuid_also_suppresses_uuid_metadata(self):
        sections = [(0x16, b"\x95\xfe\x00\xff"), (0x20, b"\x78\x56\x34\x12\xaa")]
        self.assertEqual(unpack(pack_advertisement(sections, service_uuids=["fe95", "12345678"])), sections)

    def test_duplicate_metadata_without_raw_field(self):
        result = unpack(pack_advertisement(
            [], manufacturer_data=[(1, b"a"), (1, b"a"), (1, b"b")],
            service_uuids=["fe95", "FE95", UUID("0000fe95-0000-1000-8000-00805f9b34fb")],
        ))
        self.assertEqual(result, [(0xff, b"\x01\x00a"), (0xff, b"\x01\x00b"), (0x02, b"\x95\xfe")])

    def test_boundaries_and_no_legacy_31_byte_limit(self):
        data = pack_advertisement([(0xef, b"x" * 254), (0xef, b"y" * 254)])
        self.assertEqual(len(data), 512)
        self.assertEqual(data[:2], b"\xff\xef")
        self.assertEqual(unpack(pack_advertisement([], flags=0, tx_power=-128)), [(1, b"\x00"), (10, b"\x80")])
        # Generated UUID lists can be split into individually valid structures.
        result = unpack(pack_advertisement([], service_uuids=range(128)))
        self.assertEqual([len(payload) for _, payload in result], [254, 2])
        self.assertEqual(b"".join(payload for _, payload in result), b"".join(i.to_bytes(2, "little") for i in range(128)))

    def test_overlong_raw_and_metadata_raise_never_truncate(self):
        calls = [
            lambda: pack_advertisement([(0x16, b"x" * 255)]),
            lambda: pack_advertisement([], manufacturer_data={1: b"x" * 253}),
            lambda: pack_advertisement([], local_name="中" * 85),
        ]
        for call in calls:
            with self.subTest(call=call), self.assertRaisesRegex(ValueError, "ad_too_long"):
                call()

    def test_invalid_shapes_raise_value_error(self):
        bad_sections = [None, (), [None], [(1,)], [[1, b"x"]], [(256, b"")],
                        [(-1, b"")], [(True, b"")], [(1, "hex")], [(1, 3)]]
        for sections in bad_sections:
            with self.subTest(sections=sections), self.assertRaises(ValueError):
                pack_advertisement(sections)
        bad_metadata = [dict(flags=256), dict(tx_power=-129), dict(local_name=None),
                        dict(service_uuids=["not a UUID"]), dict(service_uuids=3),
                        dict(manufacturer_data={65536: b""}), dict(manufacturer_data=[None])]
        for kwargs in bad_metadata:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                pack_advertisement([], **kwargs)


class FakeWatcher:
    def __init__(self):
        self.status = 0
        self.received = None
        self.stopped = None
        self.scanning_mode = None

    def add_received(self, handler):
        self.received = handler
        return 0  # A zero event token is valid.

    def add_stopped(self, handler):
        self.stopped = handler
        return 1

    def remove_received(self, token):
        self.received = None

    def remove_stopped(self, token):
        self.stopped = None

    def start(self):
        self.status = 1

    def stop(self):
        self.status = 3


class FakeReader:
    @classmethod
    def from_buffer(cls, buffer):
        reader = cls()
        reader.data = buffer.value
        return reader

    def read_bytes(self, target):
        target[:] = self.data

    def close(self):
        pass


def event(rssi=-50, payload=b"\x95\xfe\x00\xff"):
    return NS(
        bluetooth_address=0x112233445566, raw_signal_strength_in_dbm=rssi,
        bluetooth_address_type=1, transmit_power_level_in_dbm=None,
        advertisement=NS(
            data_sections=[NS(data_type=0x16, data=NS(length=len(payload), value=payload))],
            manufacturer_data=[], service_uuids=[], local_name="", flags=None,
        ),
    )


class ScannerOfflineTests(unittest.TestCase):
    def setUp(self):
        self.output = []
        self.stats = {}
        self.scanner = WindowsScanner(lambda *args: self.output.append(args), self.stats)
        self.load = patch("winrt_capture._load_winrt", return_value=(
            FakeWatcher, NS(CREATED=0, STARTED=1, ABORTED=4), NS(PASSIVE="passive"), FakeReader,
        ))
        self.info = patch("winrt_capture._get_adapter_info_sync", return_value={
            "address": 0xaabbccddeeff, "name": "test radio", "enabled": True,
        })
        self.load.start()
        self.info_mock = self.info.start()
        self.addCleanup(self.load.stop)
        self.addCleanup(self.info.stop)
        self.addCleanup(self.scanner.stop)

    def test_passive_raw_callback_no_packet_dedup_and_out_of_range_count(self):
        self.scanner.start()
        self.scanner.start()
        watcher = self.scanner._watcher
        self.assertEqual(watcher.scanning_mode, "passive")
        self.assertEqual(self.scanner.adapter_address, 0xaabbccddeeff)
        # Different projected sender wrappers must not cause packets to be dropped.
        watcher.received(object(), event())
        watcher.received(object(), event())
        watcher.received(object(), event(-127))
        self.assertEqual(self.output, [(0x112233445566, -50, b"\x05\x16\x95\xfe\x00\xff", 1)] * 2)
        self.assertEqual({key: self.stats[key] for key in (
            "received_events", "emitted", "invalid_advertisements", "windows_out_of_range_events"
        )}, dict(received_events=3, emitted=2, invalid_advertisements=0, windows_out_of_range_events=1))
        self.scanner.stop()
        self.scanner.stop()
        self.assertFalse(self.scanner.running)
        self.assertIsNone(watcher.received)
        self.assertIsNone(watcher.stopped)

    def test_invalid_packet_count_and_short_rate_limited_error(self):
        self.scanner.start()
        with self.assertLogs("winrt_capture", level="WARNING") as logs:
            for _ in range(2):
                self.scanner._watcher.received(None, event(payload=b"x" * 255))
        self.assertEqual(self.output, [])
        self.assertEqual(self.stats["invalid_advertisements"], 2)
        self.assertEqual(self.stats["last_error_code"], "invalid_advertisement")
        self.assertEqual(len(logs.output), 1)

    def test_asynchronous_created_to_started_transition(self):
        watcher = FakeWatcher()
        watcher.start = lambda: None
        self.load.return_value = (lambda: watcher, NS(CREATED=0, STARTED=1, ABORTED=4),
                                  NS(PASSIVE="passive"), FakeReader)
        with patch("winrt_capture.time.sleep", side_effect=lambda _: setattr(watcher, "status", 1)):
            self.scanner.start()
        self.assertTrue(self.scanner.running)
        self.assertEqual(self.stats["watcher_status"], 1)

    def test_failed_start_never_claims_running(self):
        self.info_mock.side_effect = RuntimeError("radio_disabled")
        with self.assertLogs("winrt_capture"), self.assertRaisesRegex(RuntimeError, "radio_disabled"):
            self.scanner.start()
        self.assertFalse(self.scanner.running)
        self.assertIsNone(self.scanner.adapter_address)

    def test_aborted_watcher_clears_running(self):
        self.scanner.start()
        watcher = self.scanner._watcher
        watcher.status = 4
        with self.assertLogs("winrt_capture"):
            watcher.stopped(None, NS(error=0))
        self.assertFalse(self.scanner.running)
        self.assertEqual(self.stats["last_error_code"], "watcher_aborted")

    def test_callback_failure_is_not_invalid_advertisement(self):
        self.scanner.on_advertisement = lambda *args: (_ for _ in ()).throw(RuntimeError("consumer error"))
        self.scanner.start()
        with self.assertLogs("winrt_capture"):
            self.scanner._watcher.received(None, event())
        self.assertEqual(self.stats["emitted"], 0)
        self.assertEqual(self.stats["invalid_advertisements"], 0)
        self.assertEqual(self.stats["callback_errors"], 1)


if __name__ == "__main__":
    unittest.main()

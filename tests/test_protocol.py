"""Purely synthetic offline protocol tests; no HA, network, or private files.

Run from the repository root: python -m unittest discover -s tests \
    -p test_protocol.py -v
"""
import importlib.util
from pathlib import Path
import struct
import sys
import unittest

from cryptography.hazmat.primitives.ciphers.aead import AESCCM


# Load the pure module directly: the HA integration's __init__ may import HA.
_PATH = Path(__file__).resolve().parents[1] / "custom_components/xiaomi_ble_gateway/protocol.py"
_SPEC = importlib.util.spec_from_file_location("_gateway_protocol_offline_test", _PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
decode_advertisement = _MODULE.decode_advertisement

# Public synthetic key/address, unrelated to any real device.
_KEY = bytes(range(16))
_ADDRESS = "12:34:56:78:9A:BC"


def _tlv(eid, value):
    return struct.pack("<HB", eid, len(value)) + value


def _frame(plaintext, pid=39609, control=0x5858, capability=b""):
    header = struct.pack("<HHB", control, pid, 0xFE)
    mac = bytes.fromhex(_ADDRESS.replace(":", ""))[::-1]
    extension = bytes.fromhex("010203")
    nonce = mac + header[2:5] + extension
    encrypted = AESCCM(_KEY, tag_length=4).encrypt(nonce, plaintext, b"\x11")
    return (header + (mac if control & 0x10 else b"") + capability
            + encrypted[:-4] + extension + encrypted[-4:])


def _scale(mass, heart_rate=0):
    # Fictional fixture: 512.3 ohms and an arbitrary fixed timestamp.
    packed = mass | (heart_rate << 11) | (5123 << 18)
    return _frame(_tlv(0x6E16, struct.pack("<BII", 7, packed, 1700000000)), pid=19205)


class ProtocolTests(unittest.TestCase):
    def test_co2_known_eids_and_full_counter(self):
        plain = b"".join([
            _tlv(0x601D, struct.pack("<H", 812)),
            _tlv(0x5C01, struct.pack("<f", 23.5)),
            _tlv(0x6012, b"\x38"), _tlv(0x4803, b"\x64"),
            _tlv(0xFFFF, b"ignored"),
        ])
        # Includes both capability and optional I/O capability bytes.
        result = decode_advertisement(_frame(plain, control=0x5878, capability=b"\x20\x00"), _ADDRESS, _KEY)
        self.assertEqual(result.counter, 0x030201FE)
        self.assertEqual(result.product_id, 39609)
        self.assertEqual(result.readings, {"co2": 812.0, "temperature": 23.5, "humidity": 56.0, "battery": 100.0})
        compact = decode_advertisement(_frame(plain, control=0x5848), _ADDRESS.replace(":", ""), _KEY)
        self.assertEqual(compact.readings, result.readings)
        self.assertNotIn("key", result.metadata)

    def test_scale_weight_tag_and_sentinel(self):
        result = decode_advertisement(_scale(650), _ADDRESS, _KEY)
        self.assertEqual(result.readings, {"weight_kg": 65.0})
        self.assertEqual(result.metadata["tag"], 7)
        self.assertEqual(result.metadata["device_time"], 1700000000)
        self.assertEqual(result.metadata["impedance_ohm"], 512.3)
        self.assertNotIn("profile_id", result.metadata)
        self.assertFalse(result.metadata["weight_sentinel"])
        for mass in (0, 0x7FF):
            result = decode_advertisement(_scale(mass), _ADDRESS, _KEY)
            self.assertNotIn("weight_kg", result.readings)
            self.assertNotIn("heart_rate_bpm", result.readings)
            self.assertEqual(result.metadata["mass_raw"], mass)
            self.assertEqual(result.metadata["weight_sentinel"], mass == 0x7FF)
        result = decode_advertisement(_scale(650, heart_rate=20), _ADDRESS, _KEY)
        self.assertEqual(result.readings["heart_rate_bpm"], 70.0)

    def test_basic_and_unsupported_do_not_produce_readings(self):
        mac = bytes.fromhex(_ADDRESS.replace(":", ""))[::-1]
        for pid in (39609, 19205):
            self.assertIsNone(decode_advertisement(struct.pack("<HHB", 0x5910, pid, 1) + mac, _ADDRESS, b""))
        self.assertIsNone(decode_advertisement(_frame(b"", pid=12345), _ADDRESS, _KEY))
        # A scale EID under the CO2 PID must not become a scale measurement.
        result = decode_advertisement(_frame(_tlv(0x6E16, bytes(9))), _ADDRESS, _KEY)
        self.assertEqual(result.readings, {})
        self.assertEqual(result.metadata["model"], "xiaomi.sensor_ht.new3pr")

    def test_bad_mic_or_key_rejected(self):
        raw = _scale(650)
        damaged = raw[:-1] + bytes([raw[-1] ^ 1])
        for frame, key in ((damaged, _KEY), (raw, bytes(16)), (raw, b"short")):
            with self.assertRaises(ValueError):
                decode_advertisement(frame, _ADDRESS, key)

    def test_malformed_frames_rejected(self):
        bad_plaintexts = (b"\x1d", b"\x1d\x60\x02\x01", _tlv(0x601D, b"\x01"),
                          _tlv(0x5C01, struct.pack("<f", float("nan"))))
        for raw in (b"", struct.pack("<HHB", 0x5858, 39609, 0),
                    struct.pack("<HHB", 0x5868, 39609, 0),
                    struct.pack("<HHB", 0x5868, 39609, 0) + b"\x20",
                    *(_frame(plain) for plain in bad_plaintexts),
                    _frame(_tlv(0x6E16, bytes(8)), pid=19205)):
            with self.assertRaises(ValueError):
                decode_advertisement(raw, _ADDRESS, _KEY)
        for address in ("not-a-mac", "12:34:56:78:9A:BD"):
            with self.assertRaises(ValueError):
                decode_advertisement(_scale(650), address, _KEY)

if __name__ == "__main__":
    unittest.main()

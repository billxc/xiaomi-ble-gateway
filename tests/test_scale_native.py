"""Pure synthetic native protocol/selection tests; no HA or device data."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
import unittest

_PATH = Path(__file__).resolve().parents[1] / "custom_components/xiaomi_ble_gateway"
_PACKAGE = ModuleType("_scale_native_offline_test")
_PACKAGE.__path__ = [str(_PATH)]
sys.modules[_PACKAGE.__name__] = _PACKAGE
_SPEC = importlib.util.spec_from_file_location(_PACKAGE.__name__ + ".scale_native", _PATH / "scale_native.py")
n = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = n
_SPEC.loader.exec_module(n)

TIME = 1800000000
USER = {"uid": "9007199254740993123", "accountId": "synthetic-profile",
        "accountCode": 42, "type": 1, "name": "Synthetic", "extra": {"snapshot": [1, 2]}}


def payload(*, offline=True, timestamp=TIME, status=1, idx=7):
    raw = [str(100 + i) for i in range(34)]
    raw[:7] = [USER["uid"], "42", "1", "6125", "70", str(status), str(timestamp)]
    return n.parse_native_csv(",".join(([str(idx)] if offline else []) + raw), offline=offline)


class NativeTests(unittest.TestCase):
    def select(self, records, complete=True):
        return n.select_native_record(records, device_time=TIME, received_at=TIME + 2,
                                      enabled_at=TIME - 60, gatt_result={"complete": complete})

    def build(self, record=None, user=None):
        return n.build_native_record("xiaomi.scales.ms110", "blt.synthetic",
                                     USER if user is None else user,
                                     payload() if record is None else record)

    def test_status1_all_nonzero_fields_preserved_no_bmi_or_status_rewrite(self):
        original = payload()
        built = self.build(original)
        self.assertEqual(len(built), 9)
        data = json.loads(built["data"])
        self.assertEqual(len(data), 37)
        for name, value in original["data"].items():
            self.assertEqual(data[name], value if name == "miid" else float(value), name)
        self.assertEqual(data["status"], 1)
        self.assertGreater(data["bodyResOriginZ50"], 0)
        self.assertGreater(data["bodyResOriginZ250"], 0)
        self.assertEqual(data["reportFrom"], "Offline")
        self.assertEqual(data["idx"], 7)
        self.assertEqual(built["createTime"], TIME * 1000)
        self.assertEqual(built["uid"], USER["uid"])
        self.assertEqual(built["sn"], "NO_SN")
        self.assertEqual(built["fromSource"], 2)
        self.assertEqual(data["user"], USER)
        USER["extra"]["snapshot"].append(3)
        try:
            self.assertEqual(json.loads(built["data"])["user"]["extra"]["snapshot"], [1, 2])
        finally:
            USER["extra"]["snapshot"].pop()

    def test_online_default_idx_and_explicit_sn(self):
        original = payload(offline=False, status=0)
        original["sn"] = "SYNTHETIC_SN"
        built = self.build(original)
        data = json.loads(built["data"])
        self.assertEqual((data["idx"], data["reportFrom"]), (-1, "Regular"))
        self.assertEqual(built["sn"], "SYNTHETIC_SN")

    def test_missing_fields_and_nonfinite_never_filled(self):
        for mutation in (lambda p: p["data"].pop("bmi"),
                         lambda p: p["raw_fields"].pop(),
                         lambda p: p.pop("idx"),
                         lambda p: p["data"].update(bmi="NaN"),
                         lambda p: p["raw_fields"].__setitem__(20, "Infinity")):
            original = payload()
            mutation(original)
            with self.assertRaises(ValueError):
                self.build(original)

    def test_identity_triple_conflict(self):
        for key, value in (("uid", "9007199254740993124"), ("accountCode", 43), ("type", 2)):
            with self.assertRaisesRegex(ValueError, "NATIVE_PROFILE_CONFLICT"):
                self.build(user={**USER, key: value})
        for status in (2, 3, 4, -1):
            with self.assertRaises(ValueError):
                self.build(payload(status=status))

    def test_exact_time_only_older_history_fallback_newer_conflict(self):
        self.assertEqual(self.select([payload(timestamp=TIME - 1)])["status"], "weight_only")
        self.assertEqual(self.select([payload(timestamp=TIME - 200)])["status"], "weight_only")
        self.assertEqual(self.select([payload(timestamp=TIME + 1)])["status"], "conflict")
        self.assertEqual(self.select([payload(), payload(timestamp=TIME - 1)])["status"], "native")
        self.assertEqual(self.select([payload(), payload(timestamp=TIME + 1)])["status"], "conflict")
        self.assertEqual(self.select([payload(timestamp=TIME + 123)])["status"], "weight_only")
        # Exact candidates too must be after enable and within 120 seconds.
        for received, enabled in ((TIME + 121, TIME - 1), (TIME + 2, TIME + 1)):
            result = n.select_native_record([payload()], device_time=TIME,
                received_at=received, enabled_at=enabled, gatt_result={"complete": True})
            self.assertEqual(result["status"], "weight_only")
            self.assertNotIn("record", result)

    def test_duplicate_online_offline_collapse_and_detached_snapshot(self):
        online, offline = payload(offline=False), payload()
        result = self.select([online, offline, deepcopy(offline)])
        self.assertEqual(result["status"], "native")
        self.assertTrue(result["record"]["offline"])
        offline["data"]["weight"] = "999"
        online["raw_fields"][0] = "999"
        self.assertEqual(result["record"]["data"]["weight"], "61.25")
        self.assertEqual(result["record"]["raw_fields"][0], USER["uid"])

    def test_same_time_different_raw_conflict_and_partial_gatt_no_fallback(self):
        changed = payload()
        raw = changed["raw_fields"][:]
        raw[3] = "6130"
        changed = n.parse_native_csv(",".join(raw), offline=False)
        self.assertEqual(self.select([payload(), changed])["status"], "conflict")
        self.assertEqual(self.select([payload()], complete=False)["status"], "conflict")
        self.assertEqual(self.select([], complete=False),
                         {"status": "weight_only", "reason": "gatt_failed"})
        self.assertEqual(self.select([]),
                         {"status": "weight_only", "reason": "no_fresh_native"})

    def test_status_gates(self):
        for status in (0, 1):
            self.assertEqual(self.select([payload(status=status)])["status"], "native")
        self.assertEqual(self.select([payload(status=2)]),
                         {"status": "weight_only", "reason": "native_unmatched"})
        for status in (3, 4, 99):
            self.assertEqual(self.select([payload(status=status)])["status"], "conflict")

    def test_match_requires_every_sent_field_and_snapshot(self):
        expected = self.build()
        self.assertTrue(n.native_record_matches(deepcopy(expected), expected))
        for key in expected:
            actual = deepcopy(expected)
            actual.pop(key)
            self.assertFalse(n.native_record_matches(actual, expected), key)
        data = json.loads(expected["data"])
        for key in data:
            actual = deepcopy(expected)
            altered = deepcopy(data)
            altered.pop(key)
            actual["data"] = json.dumps(altered)
            self.assertFalse(n.native_record_matches(actual, expected), key)
        for key, value in (("status", 0), ("idx", 8), ("reportFrom", "UserEditor"),
                           ("bodyResOriginZ50", 0), ("user", {**USER, "name": "changed"})):
            actual = {**expected, "data": json.dumps({**data, key: value})}
            self.assertFalse(n.native_record_matches(actual, expected), key)
        for key in ("sn", "fromSource", "dataVersion", "createTime"):
            actual = {**expected, key: "different"}
            self.assertFalse(n.native_record_matches(actual, expected), key)
        numeric = {key: float(value) if type(value) is int else value for key, value in data.items()}
        actual = {**expected, "data": json.dumps(numeric)}
        self.assertTrue(n.native_record_matches(actual, expected))


if __name__ == "__main__":
    unittest.main()

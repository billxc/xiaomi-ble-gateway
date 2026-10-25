"""Synthetic pure MS110 constructor/matcher tests; never access HA or a cloud."""
import importlib.util
import json
from pathlib import Path
import unittest

_PATH = (Path(__file__).resolve().parents[1]
         / "custom_components/xiaomi_ble_gateway/scale_weight.py")
_SPEC = importlib.util.spec_from_file_location("_scale_weight_offline_test", _PATH)
sw = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sw)


class ScaleWeightTests(unittest.TestCase):
    def setUp(self):
        self.user = {"accountId": "synthetic-profile", "uid": 123456,
                     "accountCode": 37, "type": 19, "name": "Synthetic person",
                     "originalExtra": {"preserve": [1, 2]}}

    def build(self, **changes):
        return sw.build_weight_record(**{
            "model": "xiaomi.scales.ms110", "did": "blt.synthetic",
            "user": self.user, "weight_kg": 64.1, "measurement_time": 1800000000,
            **changes})

    def test_nearest_exact_boundary_tie_and_multiple_candidates(self):
        def match(weights, value=62.5):
            return sw.nearest_weight_reference(value, [
                {"profile_id": str(i), "name": f"Person {i}", "weight": weight}
                for i, weight in enumerate(weights)])
        self.assertEqual(match([60])["status"], "matched")  # exactly 2.5
        self.assertEqual(match([60], 62.500000001)["status"], "no_match")
        self.assertEqual(match([60, 65])["status"], "ambiguous")
        self.assertEqual(match([60, 64, 62])["matched_profile_id"], "2")
        self.assertEqual(match([None, None])["status"], "no_reference")
        self.assertEqual(match([])["status"], "no_reference")
        # Decimal(str(...)), not binary float subtraction/tie tolerance.
        refs = [{"profile_id": "a", "name": "A", "weight": 60.1},
                {"profile_id": "b", "name": "B", "weight": 60.3}]
        self.assertEqual(sw.nearest_weight_reference(60.2, refs)["status"], "ambiguous")
        for invalid in (True, -1, float("nan"), float("inf"), "2.5"):
            with self.assertRaises(ValueError):
                sw.nearest_weight_reference(60, [], invalid)

    def test_history_latest_valid_usereditor_and_conflicting_latest(self):
        old = self.build(weight_kg=63, measurement_time=1799999000)
        latest = self.build(weight_kg=64.1)
        invalid = self.build(weight_kg=70, measurement_time=1800000010)
        data = json.loads(invalid["data"])
        data["weight"] = 0
        invalid["data"] = json.dumps(data)
        synthetic = self.build(weight_kg=80, measurement_time=1800000020)
        for source in ("TestCase", "TestCase_Unclaim"):
            data = json.loads(synthetic["data"])
            data["reportFrom"] = source
            synthetic["data"] = json.dumps(data)
            reference = sw.scale_history_reference(
                [invalid, synthetic, old, latest], latest, 1800000030000)
            self.assertEqual(reference, {"weight": 64.1, "reference_time": 1800000000000,
                                         "source": "history"})
        self.assertEqual(sw.scale_history_reference([latest] * 20, latest, 1800000030000),
                         reference)  # a full reference page is permitted
        conflict = self.build(weight_kg=65)
        for rows in ([latest, conflict], [dict(latest, did="other")],
                     {"records": []}, [dict(latest, createTime=1800000001000)],
                     [dict(latest, data="{}")]):
            with self.assertRaisesRegex(ValueError, "UNKNOWN"):
                sw.scale_history_reference(rows, latest, 1800000030000)
        self.assertIsNone(sw.scale_history_reference([invalid, synthetic], latest,
                                                     1800000030000))

    def test_cloud_baseline_is_explicit_and_validated(self):
        user = dict(self.user, weightTarget=65, weightUpdateTime=1800000000000)
        self.assertEqual(sw.scale_cloud_baseline(user, 1800000030000),
                         {"weight": 65.0, "reference_time": 1800000000000,
                          "source": "cloud_baseline"})
        self.assertEqual(sw.scale_cloud_baseline(dict(user, weightTarget="65.0"),
                                                1800000030000)["weight"], 65.0)
        for weight in (None, 0, -1, True, float("inf"), "invalid", "NaN", "0"):
            self.assertIsNone(sw.scale_cloud_baseline(dict(user, weightTarget=weight),
                                                      1800000030000))
        for timestamp in (None, 0, 1800000000, True, 1800000040000):
            self.assertIsNone(sw.scale_cloud_baseline(dict(user, weightUpdateTime=timestamp),
                                                      1800000030000))

    def test_official_defaults_are_placeholders_not_measured_body_fat(self):
        record = self.build()
        self.assertEqual(set(record), {"data", "dataVersion", "uid", "accountId",
                                       "createTime", "model", "did", "sn", "fromSource"})
        self.assertEqual(record["dataVersion"], 3)
        self.assertEqual(record["uid"], self.user["uid"])
        self.assertEqual(record["accountId"], self.user["accountId"])
        self.assertEqual(record["createTime"], 1800000000000)
        self.assertEqual(record["sn"], "NO_SN")
        self.assertEqual(record["fromSource"], 2)
        data = json.loads(record["data"])
        # Full official constructor, not a speculative sparse backend payload.
        zero_fields = {
            "heartRate", "bfp", "slm", "bwp", "bmc", "vfl", "pp", "smm", "bmi",
            "swt", "mc", "wc", "fc", "whr", "bmr", "bt", "ma", "sbc", "slp",
            "bmcp", "bfm", "ffm", "bwm", "pm", "bodyResOriginZ50",
            "bodyResOriginZ250", "bodyResSmoothZ50", "bodyResSmoothZ250"}
        self.assertTrue(all(data[key] == 0 for key in zero_fields))
        self.assertEqual(set(data), zero_fields | {
            "miid", "duid", "userType", "weight", "status", "time", "idx", "reportFrom", "user"})
        self.assertEqual((data["status"], data["idx"], data["reportFrom"]), (0, -1, "UserEditor"))
        self.assertEqual((data["miid"], data["duid"], data["userType"]), (123456, 37, 19))
        self.assertEqual(data["user"], self.user)
        self.assertEqual(data["time"], 1800000000)
        self.assertEqual(data["weight"], 64.1)
        self.assertEqual(self.user["originalExtra"], {"preserve": [1, 2]})

    def test_decimal_matching_identity_and_invalid_inputs(self):
        expected = self.build()
        row = dict(expected)
        data = json.loads(row["data"])
        data["weight"] = 64.100
        row["data"] = json.dumps(data)
        self.assertTrue(sw.record_matches(row, expected))
        for key, value in (("weight", 64.1001), ("miid", "other"), ("duid", 99),
                           ("userType", 0), ("reportFrom", "Offline"), ("time", 1800000001)):
            changed = dict(data, **{key: value})
            self.assertFalse(sw.record_matches(dict(row, data=json.dumps(changed)), expected))
        for key, value in (("did", "other"), ("model", "other"), ("uid", "other"),
                           ("accountId", "other"), ("createTime", 1800000001000)):
            self.assertFalse(sw.record_matches(dict(row, **{key: value}), expected))
        for value in (True, 0, -1, float("nan"), float("inf"), "64.1"):
            with self.assertRaises(ValueError):
                self.build(weight_kg=value)
        for value in (True, 0, 1800000000.0):
            with self.assertRaises(ValueError):
                self.build(measurement_time=value)
        with self.assertRaises(ValueError):
            self.build(user=dict(self.user, type="19"))
        with self.assertRaises(ValueError):
            sw.record_data(dict(row, data={}))


if __name__ == "__main__":
    unittest.main()

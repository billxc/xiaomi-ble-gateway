"""Pure offline replay-state tests; no Home Assistant or cloud access."""
import importlib.util
from pathlib import Path
import sys
import unittest

_PATH = (Path(__file__).resolve().parents[1]
         / "custom_components/xiaomi_ble_gateway/state.py")
_SPEC = importlib.util.spec_from_file_location("_gateway_state_test", _PATH)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
DeviceState = _MODULE.DeviceState
Observation = _MODULE.Observation
weight_time_is_fresh = _MODULE.weight_time_is_fresh

_CONST_SPEC = importlib.util.spec_from_file_location(
    "_gateway_const_test", _PATH.with_name("const.py"))
_CONST = importlib.util.module_from_spec(_CONST_SPEC)
_CONST_SPEC.loader.exec_module(_CONST)


class StateTests(unittest.TestCase):
    def test_first_frame_is_baseline_but_visible(self):
        state = DeviceState()
        self.assertIs(state.observe(17, {"co2": 600}), Observation.BASELINE)
        self.assertEqual(state.readings["co2"], 600)
        self.assertIsNone(state.last_attempt)
        self.assertIs(state.observe(18, {"co2": 610}), Observation.NEW)

    def test_duplicate_and_older_do_not_replace_values(self):
        state = DeviceState()
        state.observe(0x12345678, {"co2": 600})
        self.assertIs(state.observe(0x12345678, {"co2": 999}), Observation.OLD)
        self.assertIs(state.observe(0x12345677, {"co2": 998}), Observation.OLD)
        self.assertEqual(state.readings["co2"], 600)

    def test_restore_rejects_attempted_counter_without_retry(self):
        state = DeviceState()
        state.observe(100, {})
        state.observe(101, {"co2": 650})
        state.mark_attempt(101)
        restored = DeviceState.restore(state.snapshot())
        self.assertEqual(restored.cloud_status, "unknown")
        self.assertEqual(restored.last_attempt, 101)
        self.assertIs(restored.observe(101, {"co2": 650}), Observation.OLD)
        with self.assertRaises(ValueError):
            restored.mark_attempt(101)
        self.assertIs(restored.observe(102, {"co2": 660}), Observation.NEW)
        restored.mark_attempt(102)

    def test_no_weight_in_sentinel_readings_retains_last_weight(self):
        state = DeviceState()
        state.observe(1, {"weight_kg": 65.4})
        # Protocol omits weight_kg for mass_raw 0x7FF; caller merges readings.
        state.observe(2, {})
        self.assertEqual(state.readings["weight_kg"], 65.4)

    def test_no_measurements_in_persistent_snapshot(self):
        state = DeviceState()
        state.observe(1, {"weight_kg": 65.4})
        self.assertEqual(set(state.snapshot()),
                         {"counter", "last_attempt", "cloud_status", "weight_attempts"})
        self.assertEqual(DeviceState.restore(state.snapshot()).readings, {})

    def test_weight_same_time_across_counters_attempted_once(self):
        state = DeviceState()
        state.observe(100, {"weight_kg": 65.4})
        self.assertTrue(state.can_attempt_weight("profile-a", 1000))
        state.mark_weight_attempt("profile-a", 1000, 1002)
        state.observe(101, {"weight_kg": 65.4})
        self.assertFalse(state.can_attempt_weight("profile-a", 1000))
        self.assertFalse(state.can_attempt_weight("profile-a", 999))
        with self.assertRaises(ValueError):
            state.mark_weight_attempt("profile-a", 1000, 1003)
        restored = DeviceState.restore(state.snapshot())
        self.assertFalse(restored.can_attempt_weight("profile-a", 1000))
        self.assertEqual(restored.weight_attempts["profile-a"]["record_time"], 1002)
        self.assertEqual(restored.cloud_status, "unknown")
        self.assertNotIn("65.4", str(state.snapshot()))

    def test_profile_switch_preserves_device_global_consumption_boundary(self):
        state = DeviceState()
        state.mark_weight_attempt("a", 1000, 1001)
        self.assertFalse(state.can_attempt_weight("b", 1000))
        with self.assertRaises(ValueError):
            state.mark_weight_attempt("b", 1000, 1002)
        self.assertTrue(state.can_attempt_weight("b", 1001))
        state.mark_weight_attempt("b", 1001, 1002)
        self.assertFalse(state.can_attempt_weight("a", 1001))
        restored = DeviceState.restore(state.snapshot())
        self.assertEqual(set(restored.weight_attempts), {"a", "b"})
        self.assertEqual(restored.last_weight_attempt_device_time, 1001)
        self.assertFalse(restored.can_attempt_weight("new-profile", 1001))
        # JSON object order cannot affect restored global consumption boundary.
        snapshot = state.snapshot()
        snapshot["weight_attempts"] = dict(reversed(list(snapshot["weight_attempts"].items())))
        self.assertEqual(DeviceState.restore(snapshot).last_weight_attempt_device_time, 1001)

    def test_weight_freshness_boundaries_and_old_samples(self):
        self.assertTrue(weight_time_is_fresh(1000, 1005, 1120))
        self.assertTrue(weight_time_is_fresh(1120, 1005, 1000))
        self.assertFalse(weight_time_is_fresh(999, 1005, 1000))
        self.assertFalse(weight_time_is_fresh(1000, 1005, 1121))
        self.assertFalse(weight_time_is_fresh(1121, 1005, 1000))
        # Increasing counters alone cannot qualify old time after startup.
        state = DeviceState()
        self.assertIs(state.observe(100, {}), Observation.BASELINE)
        self.assertIs(state.observe(101, {}), Observation.NEW)
        self.assertFalse(weight_time_is_fresh(900, 1000, 1001))
        for device_time in (None, True, 0, -1, 1000.0):
            self.assertFalse(weight_time_is_fresh(device_time, 1000, 1001))
        self.assertFalse(weight_time_is_fresh(1000, 0, 1001))

    def test_legacy_store_preserves_co2_and_counter(self):
        restored = DeviceState.restore({"counter": 123, "last_attempt": 122,
                                        "cloud_status": "success"})
        self.assertEqual(restored.counter, 123)
        self.assertEqual(restored.last_attempt, 122)
        self.assertEqual(restored.cloud_status, "success")
        self.assertEqual(restored.weight_attempts, {})

    def test_weight_config_snapshot_preserves_fixed_and_tracks_nearest_changes(self):
        legacy = {_CONST.CONF_WEIGHT_UPLOAD: True,
                  _CONST.CONF_WEIGHT_DEVICE: "AABBCCDDEEFF",
                  _CONST.CONF_WEIGHT_PROFILE: "person-a",
                  _CONST.CONF_WEIGHT_ENABLED_AT: 1000}
        snapshot = _CONST.weight_config(legacy)
        self.assertEqual(snapshot, (True, "AABBCCDDEEFF", "person-a", 1000, "fixed", 2.5))
        self.assertEqual(len(snapshot), len(_CONST.WEIGHT_CONFIG_KEYS))
        nearest = {**legacy, _CONST.CONF_WEIGHT_MODE: "nearest"}
        self.assertNotEqual(_CONST.weight_config(nearest), snapshot)
        del nearest[_CONST.CONF_WEIGHT_PROFILE]
        self.assertEqual(_CONST.weight_config(nearest)[2:], (None, 1000, "nearest", 2.5))
        before = _CONST.weight_config(nearest)
        nearest[_CONST.CONF_WEIGHT_THRESHOLD] = 1.5
        self.assertNotEqual(_CONST.weight_config(nearest), before)
        self.assertEqual(before[-1], 2.5)  # Captured snapshots never mutate.
        legacy[_CONST.CONF_WEIGHT_PROFILE] = "person-b"
        self.assertEqual(snapshot[2], "person-a")

    def test_weight_threshold_requires_positive_finite_float(self):
        for value in (2.5, "2.5", 1, 0.01):
            with self.subTest(value=value):
                result = _CONST.positive_weight_threshold(value)
                self.assertIsInstance(result, float)
                self.assertGreater(result, 0)
        for value in (0, -1, True, False, None, "bad", "nan", float("inf"), -float("inf")):
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                _CONST.positive_weight_threshold(value)

    def test_nearest_audit_and_nonwrite_statuses_survive_restore_without_retry(self):
        state = DeviceState()
        state.mark_weight_attempt("fixed-person", 1000, 1001)
        key = _CONST.WEIGHT_NEAREST_AUDIT_KEY
        self.assertFalse(state.can_attempt_weight(key, 1000))
        state.mark_weight_attempt(key, 1001, 1002)
        for status in ("no_match", "ambiguous", "no_reference", "existing",
                       "verified", "existing_verified", "existing_conflict", "conflict", "unknown"):
            with self.subTest(status=status):
                state.cloud_status = status
                restored = DeviceState.restore(state.snapshot())
                self.assertEqual(restored.cloud_status, status)
                self.assertFalse(restored.can_attempt_weight(key, 1001))
                self.assertFalse(restored.can_attempt_weight("another-fixed-person", 1001))
                self.assertEqual(restored.weight_attempts[key], {
                    "last_weight_attempt_device_time": 1001, "record_time": 1002})
                self.assertTrue(restored.can_attempt_weight(key, 1002))
                self.assertEqual(restored.readings, {})

    def test_invalid_weight_attempt_restore_fails_closed(self):
        for attempts in ([], {"a": {}}, {"a": {
                "last_weight_attempt_device_time": True, "record_time": 1000}},
                {"a": {"last_weight_attempt_device_time": 1000,
                        "record_time": 1000, "weight_kg": 65}}):
            with self.subTest(attempts=attempts), self.assertRaises(ValueError):
                DeviceState.restore({"counter": 1, "last_attempt": None,
                                     "weight_attempts": attempts})

    def test_invalid_restore_is_not_silently_reset(self):
        for data in ({}, [], {"counter": -1, "last_attempt": None},
                     {"counter": True, "last_attempt": None},
                     {"counter": 10, "last_attempt": 11}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                DeviceState.restore(data)

    def test_wrap_and_reset_fail_closed(self):
        state = DeviceState()
        state.observe(0xFFFFFFFF, {})
        self.assertIs(state.observe(0, {}), Observation.OLD)
        self.assertIs(state.observe(10, {}), Observation.OLD)
        with self.assertRaises(ValueError):
            state.observe(0x100000000, {})


if __name__ == "__main__":
    unittest.main()

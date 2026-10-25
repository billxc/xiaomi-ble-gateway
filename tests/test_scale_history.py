"""Entirely FICTIONAL native CSV and MACs; no captures, real profiles or HA.

These arbitrary numbers exercise the protocol field map, NOT measured body
composition, a valid person's identity, or a device/cloud acceptance fixture.
"""
import importlib.util
import json
from contextlib import closing
from decimal import Decimal, localcontext
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch


_PATH = (Path(__file__).resolve().parents[1]
         / "custom_components/xiaomi_ble_gateway/scale_history.py")
_SPEC = importlib.util.spec_from_file_location("_scale_history_offline_test", _PATH)
sh = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sh)

# Locally administered fictional MACs. No measured/captured CSV is used.
MAC = "02:00:00:00:00:01"
OTHER_MAC = "02:00:00:00:00:02"
FICTIONAL_FIELDS = [
    "900719925474099312345678901234567890", "00017", "9", "7319", "83", "2",
    "2000000000", "111", "222", "333", "444", "555", "666", "777", "888",
    "999", "101", "202", "303", "404", "1234.567890123456789012345678901234567890",
    "6.125", "41", "8.75", "505", "606", "707", "808", "909", "1001",
    "12345", "23456", "34567", "45678",
]


def native(*, idx=None, **changes):
    fields = FICTIONAL_FIELDS.copy()
    for name, value in changes.items():
        fields[sh.FIELDS.index(name)] = str(value)
    parts = ([str(idx)] if idx is not None else []) + fields
    return sh.parse_native_csv(",".join(parts), offline=idx is not None)


class CodecTests(unittest.TestCase):
    def test_all_fields_scales_identity_precision_and_no_defaults(self):
        record = native()
        expected = {
            "miid": FICTIONAL_FIELDS[0], "duid": "00017", "userType": "9",
            "weight": "73.19", "heartRate": "83", "status": "2", "time": "2000000000",
            "bfp": "11.1", "slm": "22.2", "bwp": "33.3", "bmc": "44.4",
            "vfl": "55.5", "pp": "66.6", "smm": "77.7", "bmi": "88.8",
            "swt": "99.9", "mc": "10.1", "wc": "20.2", "fc": "30.3", "whr": "40.4",
            "bmr": FICTIONAL_FIELDS[20], "bt": "6.125", "ma": "41", "sbc": "8.75",
            "slp": "50.5", "bmcp": "60.6", "bfm": "70.7", "ffm": "80.8",
            "bwm": "90.9", "pm": "100.1", "bodyResOriginZ50": "1234.5",
            "bodyResOriginZ250": "2345.6", "bodyResSmoothZ50": "3456.7",
            "bodyResSmoothZ250": "4567.8",
        }
        self.assertEqual(record, {"offline": False, "raw_fields": FICTIONAL_FIELDS,
                                  "data": expected})
        self.assertEqual(json.loads(json.dumps(record)), record)
        offline = native(idx="00042")
        self.assertEqual(offline, dict(record, offline=True, idx="00042"))
        with localcontext() as context:
            context.prec = 3
            huge = native(weight="123456789012345678901234567890123456789")
            self.assertEqual(huge["data"]["weight"],
                             "1234567890123456789012345678901234567.89")
            self.assertEqual(native(), record)  # independent of decimal context

    def test_reject_incomplete_fractional_integer_and_nonfinite_input(self):
        csv = ",".join(FICTIONAL_FIELDS)
        for invalid, offline in (("1,2,3", False), (csv, True), ("42," + csv, False),
                                 (csv + ",0", False), (csv + "_" + csv, False)):
            with self.subTest(offline=offline), self.assertRaises(ValueError):
                sh.parse_native_csv(invalid, offline=offline)
        for field, invalid in (("miid", "1.1"), ("weight", "7319.5"),
                               ("time", ""), ("bmr", "NaN"), ("sbc", "Infinity"),
                               ("duid", "1e9"), ("bfp", " 123")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                native(**{field: invalid})


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "private-history" / "history.sqlite3"
        self.store = sh.HistoryStore(self.path)

    def merge(self, records, mac=MAC, source="fictional-gatt", received_at=2100000000):
        result = self.store.merge_native(mac, records, source, received_at)
        self.assertTrue(all(type(value) is int for value in result.values()))
        return result

    def broadcast(self, *, mac=MAC, time="2000000000", weight="73.19", counter=7,
                  source="fictional-scanner", received_at=2100000001):
        result = self.store.merge_broadcast(mac, time, weight, counter, source, received_at)
        self.assertTrue(all(type(value) is int for value in result.values()))
        return result

    def rows(self, sql):
        with closing(sqlite3.connect(self.path)) as connection:
            return connection.execute(sql).fetchall()

    def test_device_binding_persists_rejects_rebinding_and_preserves_history(self):
        self.assertIsNone(self.store.bind_device(MAC, "blt.fictional-scale-a"))
        self.merge([native()])
        self.broadcast()
        before = self.store.totals(MAC)
        links = self.store.query_associations(MAC)
        recovered = sh.HistoryStore(self.path)
        self.assertIsNone(recovered.bind_device("020000000001", "blt.fictional-scale-a"))
        with self.assertRaisesRegex(ValueError, "binding conflict"):
            recovered.bind_device(MAC, "blt.fictional-scale-b")
        self.assertEqual(self.rows("SELECT mac, did FROM device_bindings"),
                         [("020000000001", "blt.fictional-scale-a")])
        self.assertEqual(recovered.totals(MAC), before)
        self.assertEqual(recovered.query_associations(MAC), links)
        self.assertEqual(json.loads(self.rows("SELECT raw_json FROM native")[0][0]),
                         FICTIONAL_FIELDS)

    def test_online_offline_full_raw_identity_repeats_and_permissions(self):
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with closing(self.store._connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
        first = self.merge([native()])
        self.assertEqual(first, {"inserted": 1, "duplicates": 0, "conflicts": 0,
                                 "linked": 0, "ambiguous": 0, "unmatched": 0})
        self.assertEqual(self.merge([native(idx=42)])["duplicates"], 1)
        self.assertEqual(self.merge([native(idx=43), native()])["inserted"], 0)
        # Source, offline index, and receipt provenance survive repeats/restart.
        self.merge([native(idx=42)], source="fictional-other", received_at=2100000002)
        self.merge([native(idx=42)], received_at=2100000003)
        self.assertEqual(self.rows("SELECT DISTINCT idx FROM native_observations "
                                   "WHERE offline = 1 ORDER BY idx"), [("42",), ("43",)])
        self.assertEqual(self.rows("SELECT first_received_at, last_received_at "
                                   "FROM native_observations WHERE idx = '42' "
                                   "AND source = 'fictional-gatt'"),
                         [(2100000000, 2100000003)])
        stored_raw, stored_data = self.rows("SELECT raw_json, data_json FROM native")[0]
        self.assertEqual(json.loads(stored_raw), FICTIONAL_FIELDS)
        self.assertEqual(json.loads(stored_data), native()["data"])
        recovered = sh.HistoryStore(self.path)
        self.assertEqual(recovered.totals(MAC), {
            "native": 1, "broadcast": 0, "conflicts": 0,
            "linked": 0, "ambiguous": 0, "unmatched": 0})
        self.assertEqual(recovered.merge_native(MAC, [native(idx=42)], "restart", 2200000000)
                         ["inserted"], 0)

    def test_reused_idx_keeps_different_time_or_weight_and_is_device_local(self):
        self.merge([native(idx=42)])
        for record in (native(idx=42, time=2000000001), native(idx=42, weight=7320)):
            result = self.merge([record])
            self.assertEqual((result["inserted"], result["conflicts"]), (1, 1))
        self.assertEqual(self.store.totals(MAC)["native"], 3)
        self.assertEqual(self.store.totals(MAC)["conflicts"], 3)
        repeated = self.merge([native(idx=42, time=2000000001)])
        self.assertEqual((repeated["inserted"], repeated["duplicates"], repeated["conflicts"]),
                         (0, 1, 1))
        other = self.merge([native(idx=42, weight=9000)], mac=OTHER_MAC)
        self.assertEqual(other["conflicts"], 0)
        self.assertEqual(self.store.totals(OTHER_MAC)["native"], 1)

    def test_exact_decimal_time_and_unique_candidate_only_never_nearby(self):
        # Broadcast can arrive first; it never creates a native row.
        self.assertEqual(self.broadcast()["unmatched"], 1)
        self.assertEqual(self.store.totals(MAC)["native"], 0)
        self.assertEqual(self.merge([native()])["linked"], 1)
        # Decimal equality accepts a different textual scale, not approximation.
        self.assertEqual(self.broadcast(weight=Decimal("73.1900"))["duplicates"], 1)
        self.broadcast(time=2000000001, counter=8)
        self.broadcast(weight="73.2", counter=9)
        self.broadcast(weight="73.190000000000000000000000000001", counter=10)
        self.broadcast(mac=OTHER_MAC, counter=7)
        totals = self.store.totals(MAC)
        self.assertEqual((totals["linked"], totals["unmatched"], totals["native"]), (1, 3, 1))
        self.assertEqual(self.store.totals(OTHER_MAC)["linked"], 0)
        links = self.store.query_associations(MAC)
        self.assertIsNotNone(links[0]["native_id"])
        self.assertEqual(links[0]["association_status"], "candidate_exact")
        self.assertTrue(all(row["native_id"] is None for row in links[1:]))
        self.assertTrue(all(row["association_status"] == "unmatched" for row in links[1:]))
        # A unique exact candidate is NOT verified as the same measurement;
        # both native and broadcast records must remain independently stored.
        self.assertEqual(totals["broadcast"], 4)
        # Native data is never reduced to broadcast precision or overwritten.
        self.assertEqual(json.loads(self.rows("SELECT data_json FROM native")[0][0]),
                         native()["data"])
        self.assertEqual(sh.HistoryStore(self.path).query_associations(MAC), links)

    def test_different_complete_fields_same_pair_revoke_prior_association(self):
        self.merge([native()])
        self.broadcast()
        self.assertIsNotNone(self.store.query_associations(MAC)[0]["native_id"])
        conflicting = native(heartRate=84)
        result = self.merge([conflicting])
        self.assertEqual((result["inserted"], result["conflicts"], result["linked"],
                          result["ambiguous"]), (1, 1, 0, 1))
        self.assertEqual(self.store.query_associations(MAC)[0],
                         {"id": 1, "native_id": None, "candidate_count": 2,
                          "association_status": "ambiguous"})
        self.assertEqual(self.store.totals(MAC)["conflicts"], 2)
        self.assertEqual(self.broadcast()["conflicts"], 1)
        self.assertEqual(self.merge([native(idx=42), native(idx=43, heartRate=84)])
                         ["inserted"], 0)
        self.assertEqual(self.store.totals(MAC)["native"], 2)
        self.assertEqual(self.store.query_associations(MAC)[0]["candidate_count"], 2)
        # Both arrival orders yield ambiguity, including within one native batch.
        self.broadcast(mac=OTHER_MAC)
        summary = self.merge([native(), conflicting], mac=OTHER_MAC)
        self.assertEqual((summary["conflicts"], summary["ambiguous"]), (2, 1))
        self.assertIsNone(self.store.query_associations(OTHER_MAC)[0]["native_id"])

    def test_malformed_batch_rolls_back_and_cannot_replace_metrics(self):
        forged = native(idx=42)
        forged["data"]["weight"] = "73.2"
        with self.assertRaises(ValueError):
            self.merge([native(), forged])
        self.assertEqual(self.store.totals(MAC)["native"], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM native_observations"), [(0,)])
        self.assertEqual(self.merge([native()])["inserted"], 1)

    def test_commit_failure_raises_and_restores_previous_durable_state(self):
        self.merge([native()])
        self.broadcast()
        before = self.store.totals(MAC)
        links = self.store.query_associations(MAC)

        class FailingCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("synthetic commit failure")

        def connect_failing():
            connection = sqlite3.connect(self.path, factory=FailingCommit)
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            return connection

        with patch.object(self.store, "_connect", side_effect=connect_failing):
            # This would revoke the unique link if committed, but must roll back.
            with self.assertRaisesRegex(sqlite3.OperationalError, "synthetic commit"):
                self.merge([native(heartRate=84)])
            with self.assertRaisesRegex(sqlite3.OperationalError, "synthetic commit"):
                self.broadcast(counter=8)
        recovered = sh.HistoryStore(self.path)
        self.assertEqual(recovered.totals(MAC), before)
        self.assertEqual(recovered.query_associations(MAC), links)
        self.assertEqual(recovered.merge_native(MAC, [native(heartRate=84)], "recovery", 2200000000)
                         ["inserted"], 1)


if __name__ == "__main__":
    unittest.main()
